/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <wayland-client.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include "scene.h"
#include "video.h"
#include "present.h"

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif
#ifndef EGL_PLATFORM_WAYLAND_EXT
#define EGL_PLATFORM_WAYLAND_EXT 0x31D8
#endif
#define TEXTURE_MESSAGE 0x4d520007u
#define SCENE_MESSAGE 0x4d520008u
#define END_MESSAGE 0x444a0001u
#define BIND_MESSAGE 0x444a0002u
#define VIEW_MESSAGE 0x444a0003u
#define CURSOR_IMAGE_MESSAGE 0x444a0004u
#define CURSOR_POSITION_MESSAGE 0x444a0005u
#define MAX_MESSAGE (24u << 20)
#define OUTPUT_WIDTH 408
#define OUTPUT_HEIGHT 720
#define PARTIAL_TIMEOUT_MS 5000

struct context {
    EGLDisplay display;
    EGLContext context;
    EGLSurface surface;
    EGLConfig config;
    struct wl_display *wayland;
};
struct client {
    int fd;
    uint8_t header[8];
    size_t header_bytes, payload_bytes, capacity;
    uint8_t *payload;
    uint32_t kind, length;
    uint64_t partial_since;
    struct scene *scene;
    struct video *video;
    GLuint rendered;
    int frame_pending;
    struct context *context;
    struct present *presenter;
    int native, used_native;
    int view[5];
    uint32_t sequence;
};
static volatile sig_atomic_t stopping;
static void stop(int signal_number) { (void)signal_number; stopping = 1; }
static uint64_t milliseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000 + (unsigned long)now.tv_nsec / 1000000;
}
static void context_close(struct context *c) {
    if (c->display != EGL_NO_DISPLAY) {
        eglMakeCurrent(c->display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if (c->surface != EGL_NO_SURFACE) eglDestroySurface(c->display, c->surface);
        if (c->context != EGL_NO_CONTEXT) eglDestroyContext(c->display, c->context);
        eglTerminate(c->display);
    }
    if (c->wayland) wl_display_disconnect(c->wayland);
    memset(c, 0, sizeof *c);
}
static int context_display(struct context *c, EGLDisplay display, int window) {
    if (display == EGL_NO_DISPLAY || !eglInitialize(display, NULL, NULL)) return -1;
    c->display = display;
    if (!eglBindAPI(EGL_OPENGL_ES_API)) return -1;
    EGLint attributes[] = { EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_SURFACE_TYPE, window ? EGL_WINDOW_BIT : EGL_PBUFFER_BIT, EGL_NONE };
    EGLint count = 0, surface_type = 0;
    if (!eglChooseConfig(display, attributes, &c->config, 1, &count) || !count) return -1;
    const EGLint context_attributes[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    c->context = eglCreateContext(display, c->config, EGL_NO_CONTEXT, context_attributes);
    if (c->context == EGL_NO_CONTEXT) return -1;
    eglGetConfigAttrib(display, c->config, EGL_SURFACE_TYPE, &surface_type);
    if (surface_type & EGL_PBUFFER_BIT) {
        const EGLint dimensions[] = { EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE };
        c->surface = eglCreatePbufferSurface(display, c->config, dimensions);
        if (c->surface == EGL_NO_SURFACE) return -1;
    } else {
        const char *extensions = eglQueryString(display, EGL_EXTENSIONS);
        if (!extensions || !strstr(extensions, "EGL_KHR_surfaceless_context")) return -1;
    }
    return eglMakeCurrent(display, c->surface, c->surface, c->context) ? 0 : -1;
}
static int context_open(struct context *c) {
    memset(c, 0, sizeof *c);
    PFNEGLGETPLATFORMDISPLAYEXTPROC platform =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    // Window-compatible contexts let the compositor own the presentation buffers.
    if (getenv("XDG_RUNTIME_DIR") || getenv("WAYLAND_DISPLAY")) c->wayland = wl_display_connect(NULL);
    if (c->wayland) {
        if (!platform) setenv("EGL_PLATFORM", "wayland", 1);
        EGLDisplay display = platform ? platform(EGL_PLATFORM_WAYLAND_EXT, c->wayland, NULL) :
                                       eglGetDisplay((EGLNativeDisplayType)c->wayland);
        if (!context_display(c, display, 1)) return 0;
        fprintf(stderr, "GPU worker Wayland EGL initialization failed (0x%x)\n", eglGetError());
        context_close(c);
    }
    if (platform && !context_display(c, platform(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL), 0)) return 0;
    context_close(c);
    return -1;
}
static void client_close(struct client *c) {
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
    present_free(&c->presenter);
    scene_free(&c->scene);
    video_free(&c->video);
    free(c->payload);
    struct context *context = c->context;
    memset(c, 0, sizeof *c);
    c->context = context;
    c->fd = -1;
}
static uint32_t next_sequence(uint32_t sequence) { return sequence == UINT32_MAX ? 1 : sequence + 1; }
static int reply(struct client *c, const char *response, int length) {
    return length > 0 && send(c->fd, response, (size_t)length, MSG_NOSIGNAL) == length ? 0 : -1;
}
static void presented(void *data, uint32_t timestamp, int visible) {
    struct client *c = data;
    if (!c->frame_pending || c->fd < 0) return;
    c->sequence = next_sequence(c->sequence);
    char response[224];
    int length = snprintf(response, sizeof response,
        "{\"mode\":\"native\",\"width\":%d,\"height\":%d,\"sequence\":%u,\"presented\":%s,\"timestamp\":%u}\n",
        c->view[2], c->view[3], c->sequence, visible ? "true" : "false", timestamp);
    c->frame_pending = 0;
    if (reply(c, response, length) < 0) shutdown(c->fd, SHUT_RDWR);
}
static int bind_presenter(struct client *c) {
    if (c->frame_pending || c->length < 18 || c->length > 125 || c->payload[c->length - 1] ||
        memchr(c->payload + 16, 0, c->length - 17)) return -1;
    int rectangle[4];
    memcpy(rectangle, c->payload, sizeof rectangle);
    if (rectangle[2] < 16 || rectangle[3] < 16 || rectangle[2] > 4096 || rectangle[3] > 4096) return -1;
    present_free(&c->presenter);
    c->native = 0;
    const char *reason = "wayland_unavailable";
    if (c->context->wayland) {
        reason = "presenter_unavailable";
        c->presenter = present_create(c->context->wayland, c->context->display,
            c->context->context, c->context->config, c->context->surface);
        if (c->presenter) {
            int result = present_bind(c->presenter, (char *)c->payload + 16, rectangle);
            if (!result) {
                c->native = c->used_native = 1;
                c->view[0] = c->view[1] = c->view[4] = 0;
                c->view[2] = OUTPUT_WIDTH; c->view[3] = OUTPUT_HEIGHT;
                video_free(&c->video);
                const char response[] = "{\"event\":\"bind\",\"native\":true}\n";
                return reply(c, response, (int)sizeof response - 1);
            }
            reason = result == PRESENT_BIND_RENDERER_UNAVAILABLE ? "renderer_unavailable" :
                     result == PRESENT_BIND_LAYER_NOT_READY ? "layer_not_ready" : "bind_failed";
            fprintf(stderr, "GPU native bind: %s\n", present_error(c->presenter));
            present_free(&c->presenter);
        }
    }
    char response[128];
    int size = snprintf(response, sizeof response,
        "{\"event\":\"bind\",\"native\":false,\"reason\":\"%s\"}\n", reason);
    return size > 0 && (size_t)size < sizeof response ? reply(c, response, size) : -1;
}
static int configure_view(struct client *c) {
    if (c->length != sizeof c->view) return -1;
    int view[5]; memcpy(view, c->payload, sizeof view);
    if (view[0] < 0 || view[1] < 0 || view[2] < 16 || view[3] < 16 ||
        view[0] + (int64_t)view[2] > 4096 || view[1] + (int64_t)view[3] > 4096 || (view[4] != 0 && view[4] != 1)) return -1;
    memcpy(c->view, view, sizeof view);
    return c->native ? present_configure(c->presenter, view[0], view[1], view[2], view[3], view[4]) : 0;
}
static int cursor_image(struct client *c) {
    if (c->length < 8) return -1;
    uint32_t width, height; memcpy(&width, c->payload, 4); memcpy(&height, c->payload + 4, 4);
    if (!width || !height || width > 512 || height > 512 || 8 + (uint64_t)width * height * 4 != c->length) return -1;
    return c->native ? present_cursor_image(c->presenter, width, height, c->payload + 8) : 0;
}
static int cursor_position(struct client *c) {
    if (c->length != 20) return -1;
    float point[4]; uint32_t visible;
    memcpy(point, c->payload, sizeof point); memcpy(&visible, c->payload + 16, 4);
    for (int i = 0; i < 4; i++) if (!isfinite(point[i]) || point[i] < 0 || point[i] > 1024) return -1;
    if (visible > 1) return -1;
    if (c->native) present_cursor_position(c->presenter, point[0], point[1], (int)visible, point[2], point[3]);
    return 0;
}
static int publish_video(struct client *c) {
    if (!c->video) c->video = video_create(OUTPUT_WIDTH, OUTPUT_HEIGHT);
    if (c->video && c->used_native) video_resume_sequence(c->video, c->sequence);
    if (!c->video || video_publish(c->video, c->rendered) < 0) {
        fprintf(stderr, "%s\n", video_error()); return -1;
    }
    char response[192];
    int length = snprintf(response, sizeof response,
        "{\"path\":\"%s\",\"width\":%u,\"height\":%u,\"sequence\":%u}\n",
        video_path(c->video), OUTPUT_WIDTH, OUTPUT_HEIGHT, video_sequence(c->video));
    if (length < 0 || (size_t)length >= sizeof response ||
        send(c->fd, response, (size_t)length, MSG_NOSIGNAL) != length) return -1;
    c->sequence = video_sequence(c->video);
    c->frame_pending = 0;
    return 0;
}
static int process_message(struct client *c) {
    if (c->kind == BIND_MESSAGE) return bind_presenter(c);
    if (c->kind == VIEW_MESSAGE) return configure_view(c);
    if (c->kind == CURSOR_IMAGE_MESSAGE) return cursor_image(c);
    if (c->kind == CURSOR_POSITION_MESSAGE) return cursor_position(c);
    if (c->kind == TEXTURE_MESSAGE) {
        if (scene_texture(&c->scene, c->payload, c->length) < 0) {
            fprintf(stderr, "%s\n", scene_error()); return -1;
        }
    } else if (c->kind == SCENE_MESSAGE) {
        if (c->frame_pending || scene_frame(&c->scene, c->payload, c->length) < 0) {
            fprintf(stderr, "invalid scene batch: %s\n", scene_error()); return -1;
        }
        /* Textures retired later in the batch can still be referenced by this frame. */
        c->rendered = scene_render(c->scene);
        if (!c->rendered) { fprintf(stderr, "%s\n", scene_error()); return -1; }
        c->frame_pending = 1;
    } else if (c->kind == END_MESSAGE && !c->length && c->frame_pending) {
        if (c->native) {
            if (!present_valid(c->presenter) || present_draw(c->presenter, c->rendered, presented, c) < 0) return -1;
        } else {
            if (publish_video(c) < 0) return -1;
        }
        // Theme uploads can be much larger than the following draw lists.
        if (c->capacity > (1u << 20)) {
            free(c->payload);
            c->payload = NULL;
            c->capacity = 0;
        }
    } else {
        fprintf(stderr, "invalid GPU message or batch\n"); return -1;
    }
    return 0;
}
static int supported_message(uint32_t kind) {
    return kind == TEXTURE_MESSAGE || kind == SCENE_MESSAGE ||
           (kind >= END_MESSAGE && kind <= CURSOR_POSITION_MESSAGE);
}
static int client_read(struct client *c) {
    size_t consumed = 0;
    while (consumed < (1u << 20)) {
        uint8_t *target;
        size_t needed;
        if (c->header_bytes < sizeof c->header) {
            target = c->header + c->header_bytes;
            needed = sizeof c->header - c->header_bytes;
        } else {
            target = c->payload + c->payload_bytes;
            needed = c->length - c->payload_bytes;
        }
        ssize_t received = recv(c->fd, target, needed, 0);
        if (received <= 0) {
            if (received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return 0;
            return -1;
        }
        if (!c->partial_since) c->partial_since = milliseconds();
        consumed += (size_t)received;
        if (c->header_bytes < sizeof c->header) {
            c->header_bytes += (size_t)received;
            if (c->header_bytes != sizeof c->header) continue;
            memcpy(&c->kind, c->header, 4);
            memcpy(&c->length, c->header + 4, 4);
            if (c->length > MAX_MESSAGE || !supported_message(c->kind)) return -1;
            if (c->length > c->capacity) {
                uint8_t *payload = realloc(c->payload, c->length);
                if (!payload) return -1;
                c->payload = payload;
                c->capacity = c->length;
            }
        } else c->payload_bytes += (size_t)received;
        if (c->header_bytes == sizeof c->header && c->payload_bytes == c->length) {
            if (process_message(c) < 0) return -1;
            c->header_bytes = c->payload_bytes = 0;
            c->partial_since = 0;
        }
    }
    return 0;
}
static int activated_socket(void) {
    const char *pid = getenv("LISTEN_PID"), *fds = getenv("LISTEN_FDS");
    if (!pid || !fds || strtoul(pid, NULL, 10) != (unsigned long)getpid() || strcmp(fds, "1")) return -1;
    struct sockaddr_un address;
    socklen_t length = sizeof address;
    int accepting = 0;
    socklen_t option_length = sizeof accepting;
    if (getsockname(3, (struct sockaddr *)&address, &length) < 0 || address.sun_family != AF_UNIX ||
        getsockopt(3, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &option_length) < 0 || !accepting) return -1;
    fcntl(3, F_SETFD, FD_CLOEXEC);
    return 3;
}
static int create_socket(const char *path) {
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    if (strlen(path) >= sizeof address.sun_path) return -1;
    strcpy(address.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (bind(fd, (struct sockaddr *)&address, sizeof address) < 0) { close(fd); return -1; }
    if (chmod(path, 0600) < 0 || listen(fd, 4) < 0) { close(fd); unlink(path); return -1; }
    return fd;
}
int main(int argc, char **argv) {
    const char *socket_path = NULL;
    int listener;
    if (argc == 3 && !strcmp(argv[1], "--socket")) {
        socket_path = argv[2];
        listener = create_socket(socket_path);
    } else if (argc == 1) listener = activated_socket();
    else { fprintf(stderr, "usage: doodle-gpu [--socket PATH]\n"); return 2; }
    if (listener < 0) { fprintf(stderr, "GPU worker needs a listening Unix socket\n"); return 1; }
    if (fcntl(listener, F_SETFL, fcntl(listener, F_GETFL) | O_NONBLOCK) < 0) { close(listener); return 1; }
    signal(SIGINT, stop);
    signal(SIGTERM, stop);
    signal(SIGPIPE, SIG_IGN);
    struct context context;
    if (context_open(&context) < 0) {
        fprintf(stderr, "GPU worker cannot create an offscreen EGL context (0x%x)\n", eglGetError());
        close(listener);
        if (socket_path) unlink(socket_path);
        return 1;
    }
    const char *renderer = (const char *)glGetString(GL_RENDERER);
    fprintf(stderr, "GPU worker EGL: %s, renderer: %s\n",
        context.wayland ? "Wayland" : "surfaceless", renderer ? renderer : "unknown");
    if (scene_init() < 0) {
        fprintf(stderr, "%s\n", scene_error());
        context_close(&context); close(listener);
        if (socket_path) unlink(socket_path);
        return 1;
    }
    struct client client = { .fd = -1, .context = &context };
    int result = 0;
    while (!stopping) {
        if (client.presenter && present_dispatch(client.presenter) < 0) { result = 1; break; }
        struct pollfd descriptors[] = { { listener, POLLIN, 0 }, { client.fd, POLLIN, 0 },
            { client.presenter ? present_fd(client.presenter) : -1, POLLIN, 0 } };
        int ready = poll(descriptors, 3, 1000);
        if (ready < 0 && errno != EINTR) { result = 1; break; }
        if (ready < 0) continue;
        if (descriptors[0].revents & POLLIN) {
            int incoming = accept4(listener, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
            if (incoming >= 0) {
                /* A fresh game connection replaces an abandoned one, including after a client crash. */
                client_close(&client);
                client.fd = incoming;
            }
        }
        if (client.fd >= 0 && descriptors[1].fd == client.fd &&
            ((descriptors[1].revents & (POLLERR | POLLHUP | POLLNVAL)) ||
             ((descriptors[1].revents & POLLIN) && client_read(&client) < 0))) client_close(&client);
        if (client.presenter && (descriptors[2].revents & (POLLIN | POLLERR | POLLHUP)) && present_dispatch(client.presenter) < 0) { result = 1; break; }
        if (client.fd >= 0 && client.partial_since && milliseconds() - client.partial_since > PARTIAL_TIMEOUT_MS) client_close(&client);
    }
    client_close(&client);
    context_close(&context);
    close(listener);
    if (socket_path) unlink(socket_path);
    return result;
}

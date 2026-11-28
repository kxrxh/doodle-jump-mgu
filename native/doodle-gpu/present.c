/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _GNU_SOURCE
#include "present.h"
#include "ivi-application-client-protocol.h"
#include "ivi-controller-client-protocol.h"
#include <GLES2/gl2.h>
#include <dirent.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wayland-egl.h>

#define MAX_OBJECTS 128
#define ROUNDTRIP_MS 750
struct present;
struct layer {
    struct present *owner;
    uint32_t id;
    struct ivi_controller_layer *proxy;
    int rectangle[4], visible, on_screen, alive;
};
struct watched_surface {
    struct present *owner;
    uint32_t id, pid;
    struct ivi_controller_surface *proxy;
    struct ivi_controller_layer *parent;
    int alive;
};
struct present {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct ivi_application *application;
    struct ivi_controller *controller;
    struct wl_output *outputs[16];
    struct ivi_controller_screen *screens[16];
    unsigned output_count, screen_count;
    struct layer layers[MAX_OBJECTS];
    struct watched_surface surfaces[MAX_OBJECTS];
    unsigned layer_count, surface_count;
    struct layer *parent;
    struct wl_surface *surface;
    struct ivi_surface *ivi_surface;
    struct ivi_controller_surface *control;
    uint32_t own_id;
    struct wl_egl_window *window;
    struct wl_callback *callback;
    EGLDisplay egl;
    EGLContext context;
    EGLConfig config;
    EGLSurface idle, target;
    GLuint program, vertices, cursor;
    GLint viewport_uniform, rectangle_uniform, flip_uniform;
    unsigned cursor_width, cursor_height;
    float cursor_x, cursor_y, hotspot_x, hotspot_y;
    int cursor_visible, x, y, width, height, show, valid;
    present_done done;
    void *done_data;
    char error[192];
};
static int fail(struct present *p, const char *message) {
    snprintf(p->error, sizeof p->error, "%s", message);
    return -1;
}
const char *present_error(const struct present *p) {
    return p ? p->error : "native presentation unavailable";
}
static uint64_t milliseconds(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000;
}
static void synced(void *data, struct wl_callback *callback, uint32_t serial) {
    (void)callback;
    (void)serial;
    *(int *)data = 1;
}
static const struct wl_callback_listener sync_listener = {synced};
static int dispatch_wait(struct present *p, int timeout) {
    while (wl_display_prepare_read(p->display) != 0)
        if (wl_display_dispatch_pending(p->display) < 0)
            return fail(p, "Wayland event dispatch failed");
    int flush = wl_display_flush(p->display);
    if (flush < 0 && errno != EAGAIN) {
        wl_display_cancel_read(p->display);
        return fail(p, "Wayland connection closed");
    }
    struct pollfd fd = {wl_display_get_fd(p->display), POLLIN | (flush < 0 ? POLLOUT : 0), 0};
    int result;
    do
        result = poll(&fd, 1, timeout);
    while (result < 0 && errno == EINTR);
    if (result > 0 && (fd.revents & POLLIN)) {
        if (wl_display_read_events(p->display) < 0)
            return fail(p, "Wayland read failed");
    } else
        wl_display_cancel_read(p->display);
    if (result < 0 || (fd.revents & (POLLHUP | POLLERR | POLLNVAL)))
        return fail(p, "Wayland connection lost");
    if (wl_display_dispatch_pending(p->display) < 0)
        return fail(p, "Wayland event dispatch failed");
    return 0;
}
static int roundtrip(struct present *p) {
    int complete = 0;
    struct wl_callback *callback = wl_display_sync(p->display);
    if (!callback)
        return fail(p, "Wayland sync allocation failed");
    wl_callback_add_listener(callback, &sync_listener, &complete);
    uint64_t deadline = milliseconds() + ROUNDTRIP_MS;
    int result = 0;
    while (!complete) {
        uint64_t now = milliseconds();
        if (now >= deadline) {
            result = fail(p, "Wayland compositor did not answer");
            break;
        }
        if (dispatch_wait(p, (int)(deadline - now)) < 0) {
            result = -1;
            break;
        }
    }
    wl_callback_destroy(callback);
    return result;
}
static void complete_frame(struct present *p, uint32_t stamp, int presented) {
    present_done done = p->done;
    void *data = p->done_data;
    p->done = NULL;
    p->done_data = NULL;
    if (p->callback) {
        wl_callback_destroy(p->callback);
        p->callback = NULL;
    }
    if (done)
        done(data, stamp, presented);
}
void present_hide(struct present *p) {
    if (!p)
        return;
    p->show = 0;
    complete_frame(p, 0, 0);
    if (p->control) {
        ivi_controller_surface_set_visibility(p->control, 0);
        ivi_controller_commit_changes(p->controller);
        wl_display_flush(p->display);
    }
}
static void layer_visibility(void *data, struct ivi_controller_layer *proxy, int32_t value) {
    (void)proxy;
    struct layer *layer = data;
    layer->visible = value != 0;
    if (!value && layer->owner->parent == layer)
        complete_frame(layer->owner, 0, 0);
}
static void layer_fixed(void *data, struct ivi_controller_layer *proxy, wl_fixed_t value) {
    (void)data;
    (void)proxy;
    (void)value;
}
static void layer_source(void *data, struct ivi_controller_layer *proxy, int32_t x, int32_t y, int32_t w,
                         int32_t h) {
    (void)data;
    (void)proxy;
    (void)x;
    (void)y;
    (void)w;
    (void)h;
}
static void layer_destination(void *data, struct ivi_controller_layer *proxy, int32_t x, int32_t y, int32_t w,
                              int32_t h) {
    (void)proxy;
    struct layer *layer = data;
    layer->rectangle[0] = x;
    layer->rectangle[1] = y;
    layer->rectangle[2] = w;
    layer->rectangle[3] = h;
}
static void layer_configuration(void *data, struct ivi_controller_layer *proxy, int32_t w, int32_t h) {
    (void)data;
    (void)proxy;
    (void)w;
    (void)h;
}
static void layer_integer(void *data, struct ivi_controller_layer *proxy, int32_t value) {
    (void)data;
    (void)proxy;
    (void)value;
}
static void layer_screen(void *data, struct ivi_controller_layer *proxy, struct wl_output *output) {
    (void)proxy;
    struct layer *layer = data;
    layer->on_screen = output != NULL;
    if (!output && layer->owner->parent == layer)
        complete_frame(layer->owner, 0, 0);
}
static void layer_destroyed(void *data, struct ivi_controller_layer *proxy) {
    struct layer *layer = data;
    layer->alive = 0;
    if (layer->owner->parent == layer) {
        layer->owner->valid = 0;
        present_hide(layer->owner);
    }
    ivi_controller_layer_destroy(proxy, 0);
    layer->proxy = NULL;
}
static const struct ivi_controller_layer_listener layer_listener = {
    layer_visibility,    layer_fixed,   layer_source, layer_destination,
    layer_configuration, layer_integer, layer_screen, layer_destroyed};
static void surface_integer(void *data, struct ivi_controller_surface *proxy, int32_t value) {
    (void)data;
    (void)proxy;
    (void)value;
}
static void surface_fixed(void *data, struct ivi_controller_surface *proxy, wl_fixed_t value) {
    (void)data;
    (void)proxy;
    (void)value;
}
static void surface_rectangle(void *data, struct ivi_controller_surface *proxy, int32_t x, int32_t y,
                              int32_t w, int32_t h) {
    (void)data;
    (void)proxy;
    (void)x;
    (void)y;
    (void)w;
    (void)h;
}
static void surface_configuration(void *data, struct ivi_controller_surface *proxy, int32_t w, int32_t h) {
    (void)data;
    (void)proxy;
    (void)w;
    (void)h;
}
static void surface_layer(void *data, struct ivi_controller_surface *proxy,
                          struct ivi_controller_layer *layer) {
    (void)proxy;
    ((struct watched_surface *)data)->parent = layer;
}
static void surface_stats(void *data, struct ivi_controller_surface *proxy, uint32_t redraw, uint32_t frames,
                          uint32_t updates, uint32_t pid, const char *name) {
    (void)proxy;
    (void)redraw;
    (void)frames;
    (void)updates;
    (void)name;
    ((struct watched_surface *)data)->pid = pid;
}
static void surface_destroyed(void *data, struct ivi_controller_surface *proxy) {
    struct watched_surface *surface = data;
    surface->alive = 0;
    ivi_controller_surface_destroy(proxy, 0);
    surface->proxy = NULL;
}
static const struct ivi_controller_surface_listener surface_listener = {
    surface_integer,       surface_fixed,     surface_rectangle, surface_rectangle,
    surface_configuration, surface_integer,   surface_integer,   surface_layer,
    surface_stats,         surface_destroyed, surface_integer};
static void controller_screen(void *data, struct ivi_controller *controller, uint32_t id,
                              struct ivi_controller_screen *screen) {
    (void)controller;
    (void)id;
    struct present *p = data;
    if (p->screen_count < 16)
        p->screens[p->screen_count++] = screen;
    else
        ivi_controller_screen_destroy(screen);
}
static void controller_layer(void *data, struct ivi_controller *controller, uint32_t id) {
    struct present *p = data;
    for (unsigned i = 0; i < p->layer_count; i++)
        if (p->layers[i].id == id && p->layers[i].alive)
            return;
    if (p->layer_count == MAX_OBJECTS)
        return;
    struct layer *layer = &p->layers[p->layer_count++];
    layer->owner = p;
    layer->id = id;
    layer->alive = 1;
    layer->proxy = ivi_controller_layer_create(controller, id, 0, 0);
    ivi_controller_layer_add_listener(layer->proxy, &layer_listener, layer);
}
static void controller_surface(void *data, struct ivi_controller *controller, uint32_t id) {
    struct present *p = data;
    /* The native surface already has an owning control proxy. */
    if (id == p->own_id)
        return;
    for (unsigned i = 0; i < p->surface_count; i++)
        if (p->surfaces[i].id == id && p->surfaces[i].alive)
            return;
    if (p->surface_count == MAX_OBJECTS)
        return;
    struct watched_surface *surface = &p->surfaces[p->surface_count++];
    surface->owner = p;
    surface->id = id;
    surface->alive = 1;
    surface->proxy = ivi_controller_surface_create(controller, id);
    ivi_controller_surface_add_listener(surface->proxy, &surface_listener, surface);
    ivi_controller_surface_send_stats(surface->proxy);
}
static void controller_error(void *data, struct ivi_controller *controller, int32_t object, int32_t type,
                             int32_t code, const char *text) {
    (void)controller;
    (void)object;
    (void)type;
    (void)code;
    struct present *p = data;
    fail(p, text ? text : "IVI compositor error");
    p->valid = 0;
}
static const struct ivi_controller_listener controller_listener = {controller_screen, controller_layer,
                                                                   controller_surface, controller_error};
static void output_geometry(void *data, struct wl_output *output, int32_t x, int32_t y, int32_t pw,
                            int32_t ph, int32_t subpixel, const char *make, const char *model,
                            int32_t transform) {
    (void)data;
    (void)output;
    (void)x;
    (void)y;
    (void)pw;
    (void)ph;
    (void)subpixel;
    (void)make;
    (void)model;
    (void)transform;
}
static void output_mode(void *data, struct wl_output *output, uint32_t flags, int32_t w, int32_t h,
                        int32_t refresh) {
    (void)data;
    (void)output;
    (void)flags;
    (void)w;
    (void)h;
    (void)refresh;
}
static const struct wl_output_listener output_listener = {.geometry = output_geometry, .mode = output_mode};
static void registry_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface,
                            uint32_t version) {
    (void)version;
    struct present *p = data;
    if (!strcmp(interface, "wl_compositor") && !p->compositor)
        p->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
    else if (!strcmp(interface, "ivi_application") && !p->application)
        p->application = wl_registry_bind(registry, name, &ivi_application_interface, 1);
    else if (!strcmp(interface, "ivi_controller") && !p->controller) {
        p->controller = wl_registry_bind(registry, name, &ivi_controller_interface, 1);
        ivi_controller_add_listener(p->controller, &controller_listener, p);
    } else if (!strcmp(interface, "wl_output") && p->output_count < 16) {
        struct wl_output *output = wl_registry_bind(registry, name, &wl_output_interface, 1);
        wl_output_add_listener(output, &output_listener, p);
        p->outputs[p->output_count++] = output;
    }
}
static void registry_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}
static const struct wl_registry_listener registry_listener = {registry_global, registry_remove};
static GLuint compile_shader(GLenum kind, const char *source) {
    GLuint shader = glCreateShader(kind);
    GLint compiled;
    glShaderSource(shader, 1, &source, NULL);
    glCompileShader(shader);
    glGetShaderiv(shader, GL_COMPILE_STATUS, &compiled);
    if (!compiled) {
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}
static int setup_graphics(struct present *p) {
    const char *vertex = "attribute vec2 point;uniform vec2 viewport;uniform vec4 "
                         "rectangle;uniform float flip;varying vec2 uv;void main(){vec2 "
                         "position=rectangle.xy+point*rectangle.zw;gl_Position=vec4(position.x*2."
                         "0/viewport.x-1.0,1.0-position.y*2.0/"
                         "viewport.y,0.0,1.0);uv=vec2(point.x,mix(point.y,1.0-point.y,flip));}";
    const char *fragment = "precision mediump float;uniform sampler2D image;varying vec2 uv;void "
                           "main(){gl_FragColor=texture2D(image,uv);}";
    GLuint vs = compile_shader(GL_VERTEX_SHADER, vertex), fs = compile_shader(GL_FRAGMENT_SHADER, fragment);
    if (!vs || !fs) {
        if (vs)
            glDeleteShader(vs);
        if (fs)
            glDeleteShader(fs);
        return fail(p, "native blit shader compilation failed");
    }
    p->program = glCreateProgram();
    glAttachShader(p->program, vs);
    glAttachShader(p->program, fs);
    glBindAttribLocation(p->program, 0, "point");
    glLinkProgram(p->program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint linked;
    glGetProgramiv(p->program, GL_LINK_STATUS, &linked);
    if (!linked)
        return fail(p, "native blit shader link failed");
    p->viewport_uniform = glGetUniformLocation(p->program, "viewport");
    p->rectangle_uniform = glGetUniformLocation(p->program, "rectangle");
    p->flip_uniform = glGetUniformLocation(p->program, "flip");
    glUseProgram(p->program);
    glUniform1i(glGetUniformLocation(p->program, "image"), 0);
    glUseProgram(0);
    static const GLfloat vertices[] = {0, 0, 1, 0, 0, 1, 1, 1};
    glGenBuffers(1, &p->vertices);
    glBindBuffer(GL_ARRAY_BUFFER, p->vertices);
    glBufferData(GL_ARRAY_BUFFER, sizeof vertices, vertices, GL_STATIC_DRAW);
    glBindBuffer(GL_ARRAY_BUFFER, 0);
    return glGetError() == GL_NO_ERROR ? 0 : fail(p, "native blit allocation failed");
}
struct present *present_create(struct wl_display *display, EGLDisplay egl, EGLContext context,
                               EGLConfig config, EGLSurface idle) {
    if (!display)
        return NULL;
    struct present *p = calloc(1, sizeof *p);
    if (!p)
        return NULL;
    p->display = display;
    p->egl = egl;
    p->context = context;
    p->config = config;
    p->idle = idle;
    p->target = EGL_NO_SURFACE;
    p->registry = wl_display_get_registry(display);
    wl_registry_add_listener(p->registry, &registry_listener, p);
    if (roundtrip(p) < 0 || roundtrip(p) < 0 || !p->compositor || !p->application || !p->controller ||
        setup_graphics(p) < 0) {
        fprintf(stderr, "GPU native presenter: %s\n",
            p->error[0] ? present_error(p) : "required Wayland/IVI globals missing");
        present_free(&p);
        return NULL;
    }
    return p;
}
static unsigned long listener_inode(const char *path) {
    FILE *file = fopen("/proc/net/unix", "r");
    if (!file) return 0;
    char line[512], name[108];
    unsigned long flags, inode, found = 0;
    while (fgets(line, sizeof line, file)) {
        if (sscanf(line, "%*s %*s %*s %lx %*s %*s %lu %107s", &flags, &inode, name) == 3 &&
            (flags & 0x10000) && !strcmp(name, path)) {
            if (found) { found = 0; break; }
            found = inode;
        }
    }
    fclose(file);
    return found;
}
static int holds_listener(uint32_t pid, unsigned long inode) {
    if (!inode || !pid) return 0;
    char directory[64], target[64], expected[64], file[128];
    snprintf(directory, sizeof directory, "/proc/%u/fd", pid);
    DIR *fds = opendir(directory);
    if (!fds) return 0;
    snprintf(expected, sizeof expected, "socket:[%lu]", inode);
    struct dirent *entry;
    int found = 0;
    while ((entry = readdir(fds))) {
        if (entry->d_name[0] < '0' || entry->d_name[0] > '9') continue;
        if (snprintf(file, sizeof file, "%s/%s", directory, entry->d_name) >= (int)sizeof file) continue;
        ssize_t size = readlink(file, target, sizeof target - 1);
        if (size < 0) continue;
        target[size] = 0;
        if (!strcmp(target, expected)) { found = 1; break; }
    }
    closedir(fds);
    return found;
}
static int renderer_pid(struct present *p, const char *path, unsigned long *inode) {
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (!path || path[0] != '/' || strlen(path) >= sizeof address.sun_path)
        return fail(p, "invalid renderer socket path");
    strcpy(address.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0)
        return fail(p, "renderer socket allocation failed");
    int result = connect(fd, (struct sockaddr *)&address, sizeof address);
    if (result < 0 && errno == EINPROGRESS) {
        struct pollfd pollfd = {fd, POLLOUT, 0};
        result = poll(&pollfd, 1, ROUNDTRIP_MS) > 0 ? 0 : -1;
    }
    struct ucred peer;
    socklen_t size = sizeof peer;
    if (result < 0 || getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) < 0) {
        close(fd);
        return fail(p, "renderer socket did not answer");
    }
    close(fd);
    /* Socket activation reports its creator, not the service holding the listener. */
    *inode = listener_inode(path);
    return peer.pid;
}
static void destroy_window(struct present *p) {
    present_hide(p);
    eglMakeCurrent(p->egl, p->idle, p->idle, p->context);
    if (p->target != EGL_NO_SURFACE) {
        eglDestroySurface(p->egl, p->target);
        p->target = EGL_NO_SURFACE;
    }
    if (p->window) {
        wl_egl_window_destroy(p->window);
        p->window = NULL;
    }
    if (p->control) {
        ivi_controller_surface_destroy(p->control, 1);
        p->control = NULL;
    }
    if (p->ivi_surface) {
        ivi_surface_destroy(p->ivi_surface);
        p->ivi_surface = NULL;
    }
    if (p->surface) {
        wl_surface_destroy(p->surface);
        p->surface = NULL;
    }
    p->valid = 0;
    p->parent = NULL;
    if (p->controller)
        ivi_controller_commit_changes(p->controller);
}
int present_bind(struct present *p, const char *renderer_socket, const int rectangle[4]) {
    if (!p)
        return -1;
    destroy_window(p);
    unsigned long inode = 0;
    int pid = renderer_pid(p, renderer_socket, &inode);
    if (pid < 0)
        return PRESENT_BIND_RENDERER_UNAVAILABLE;
    if (roundtrip(p) < 0)
        return -1;
    /* Outputs and layer proxies must exist before the compositor sends parent
     * events. */
    for (unsigned i = 0; i < p->layer_count; i++) {
        struct layer *layer = &p->layers[i];
        if (!layer->alive)
            continue;
        if (layer->proxy)
            ivi_controller_layer_destroy(layer->proxy, 0);
        layer->proxy = ivi_controller_layer_create(p->controller, layer->id, 0, 0);
        ivi_controller_layer_add_listener(layer->proxy, &layer_listener, layer);
    }
    if (roundtrip(p) < 0)
        return -1;
    struct layer *candidate = NULL;
    unsigned count = 0, watched_layers = 0, watched_surfaces = 0;
    uint64_t deadline = milliseconds() + ROUNDTRIP_MS;
    for (;;) {
        if (watched_layers != p->layer_count || watched_surfaces != p->surface_count) {
            for (unsigned i = 0; i < p->surface_count; i++) {
                struct watched_surface *surface = &p->surfaces[i];
                if (!surface->alive)
                    continue;
                if (surface->proxy)
                    ivi_controller_surface_destroy(surface->proxy, 0);
                surface->parent = NULL;
                surface->proxy = ivi_controller_surface_create(p->controller, surface->id);
                ivi_controller_surface_add_listener(surface->proxy, &surface_listener, surface);
                ivi_controller_surface_send_stats(surface->proxy);
            }
            watched_layers = p->layer_count;
            watched_surfaces = p->surface_count;
            if (roundtrip(p) < 0)
                return -1;
        }
        candidate = NULL;
        count = 0;
        for (unsigned i = 0; i < p->layer_count; i++) {
            struct layer *layer = &p->layers[i];
            if (!layer->alive || memcmp(layer->rectangle, rectangle, sizeof layer->rectangle))
                continue;
            int owned = 0;
            for (unsigned j = 0; j < p->surface_count; j++)
                owned |= p->surfaces[j].alive && p->surfaces[j].parent == layer->proxy &&
                         (p->surfaces[j].pid == (uint32_t)pid ||
                          holds_listener(p->surfaces[j].pid, inode));
            if (owned) {
                candidate = layer;
                count++;
            }
        }
        if (count || milliseconds() >= deadline)
            break;
        if (dispatch_wait(p, 20) < 0)
            return -1;
    }
    if (count != 1) {
        fail(p, "renderer layer challenge did not identify one parent");
        return count ? -1 : PRESENT_BIND_LAYER_NOT_READY;
    }
    p->parent = candidate;
    uint32_t id = 0x444a0000u | ((uint32_t)getpid() & 0xffffu);
    for (unsigned attempts = 0; attempts < MAX_OBJECTS; attempts++, id++) {
        int exists = 0;
        for (unsigned i = 0; i < p->surface_count; i++)
            exists |= p->surfaces[i].alive && p->surfaces[i].id == id;
        if (!exists)
            break;
        if (attempts + 1 == MAX_OBJECTS)
            return fail(p, "native surface id space busy");
    }
    p->width = (int)lround(rectangle[3] * 544.0 / 960.0);
    p->height = rectangle[3];
    p->x = (rectangle[2] - p->width) / 2;
    p->y = 0;
    if (p->width < 1 || p->height < 1 || p->width > 4096 || p->height > 4096)
        return fail(p, "native viewport outside supported bounds");
    p->surface = wl_compositor_create_surface(p->compositor);
    p->own_id = id;
    struct wl_region *empty = wl_compositor_create_region(p->compositor);
    wl_surface_set_input_region(p->surface, empty);
    wl_region_destroy(empty);
    p->ivi_surface = ivi_application_surface_create(p->application, id, p->surface);
    p->window = wl_egl_window_create(p->surface, p->width, p->height);
    p->target = eglCreateWindowSurface(p->egl, p->config, (EGLNativeWindowType)p->window, NULL);
    if (p->target == EGL_NO_SURFACE || !eglMakeCurrent(p->egl, p->target, p->target, p->context)) {
        destroy_window(p);
        return fail(p, "native EGL window creation failed");
    }
    eglSwapInterval(p->egl, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glClearColor(0, 0, 0, 0);
    glClear(GL_COLOR_BUFFER_BIT);
    if (!eglSwapBuffers(p->egl, p->target) || roundtrip(p) < 0) {
        destroy_window(p);
        return fail(p, "native window initialization failed");
    }
    p->control = ivi_controller_surface_create(p->controller, id);
    ivi_controller_surface_set_source_rectangle(p->control, 0, 0, p->width, p->height);
    ivi_controller_surface_set_destination_rectangle(p->control, p->x, p->y, p->width, p->height);
    ivi_controller_surface_set_visibility(p->control, 0);
    ivi_controller_layer_add_surface(p->parent->proxy, p->control);
    ivi_controller_commit_changes(p->controller);
    if (roundtrip(p) < 0) {
        destroy_window(p);
        return -1;
    }
    p->valid = 1;
    eglMakeCurrent(p->egl, p->idle, p->idle, p->context);
    return 0;
}
int present_configure(struct present *p, int x, int y, int width, int height, int show) {
    if (!p || !p->valid)
        return -1;
    if (width < 1 || height < 1 || width > 4096 || height > 4096 || x < 0 || y < 0 || x > 16384 || y > 16384)
        return fail(p, "invalid native viewport");
    if (p->width != width || p->height != height) {
        complete_frame(p, 0, 0);
        wl_egl_window_resize(p->window, width, height, 0, 0);
    }
    p->x = x;
    p->y = y;
    p->width = width;
    p->height = height;
    p->show = show != 0;
    ivi_controller_surface_set_source_rectangle(p->control, 0, 0, width, height);
    ivi_controller_surface_set_destination_rectangle(p->control, x, y, width, height);
    ivi_controller_surface_set_visibility(p->control, p->show);
    ivi_controller_commit_changes(p->controller);
    if (!show)
        complete_frame(p, 0, 0);
    wl_display_flush(p->display);
    return 0;
}
static void frame_done(void *data, struct wl_callback *callback, uint32_t stamp) {
    (void)callback;
    struct present *p = data;
    complete_frame(p, stamp, p->valid && p->show && p->parent->visible && p->parent->on_screen);
}
static const struct wl_callback_listener frame_listener = {frame_done};
static void quad(struct present *p, GLuint texture, float x, float y, float w, float h, int flip) {
    glBindTexture(GL_TEXTURE_2D, texture);
    glUniform4f(p->rectangle_uniform, x, y, w, h);
    glUniform1f(p->flip_uniform, (float)flip);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}
int present_draw(struct present *p, unsigned texture, present_done done, void *data) {
    if (!p || !p->valid || !texture || p->callback)
        return -1;
    if (!p->show || !p->parent->visible || !p->parent->on_screen) {
        if (done)
            done(data, 0, 0);
        return 0;
    }
    if (!eglMakeCurrent(p->egl, p->target, p->target, p->context))
        return fail(p, "native EGL context lost");
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glViewport(0, 0, p->width, p->height);
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_BLEND);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glUseProgram(p->program);
    glUniform2f(p->viewport_uniform, (float)p->width, (float)p->height);
    glActiveTexture(GL_TEXTURE0);
    glBindBuffer(GL_ARRAY_BUFFER, p->vertices);
    glEnableVertexAttribArray(0);
    glDisableVertexAttribArray(1);
    glDisableVertexAttribArray(2);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
    quad(p, texture, 0, 0, (float)p->width, (float)p->height, 1);
    if (p->cursor && p->cursor_visible) {
        float scale = 32.0f * p->height / 720.0f / p->cursor_height;
        glEnable(GL_BLEND);
        glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
        quad(p, p->cursor, p->cursor_x * p->width / 544.0f - p->hotspot_x * scale,
             p->cursor_y * p->height / 960.0f - p->hotspot_y * scale, p->cursor_width * scale,
             p->cursor_height * scale, 0);
    }
    if (glGetError() != GL_NO_ERROR) {
        eglMakeCurrent(p->egl, p->idle, p->idle, p->context);
        return fail(p, "native GPU draw failed");
    }
    p->done = done;
    p->done_data = data;
    p->callback = wl_surface_frame(p->surface);
    wl_callback_add_listener(p->callback, &frame_listener, p);
    int swapped = eglSwapBuffers(p->egl, p->target);
    eglMakeCurrent(p->egl, p->idle, p->idle, p->context);
    if (!swapped) {
        complete_frame(p, 0, 0);
        return fail(p, "native swap failed");
    }
    wl_display_flush(p->display);
    return 0;
}
int present_cursor_image(struct present *p, unsigned width, unsigned height, const uint8_t *rgba) {
    if (!p || !rgba || !width || !height || width > 512 || height > 512)
        return -1;
    if (!p->cursor)
        glGenTextures(1, &p->cursor);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, p->cursor);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, (GLsizei)width, (GLsizei)height, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                 rgba);
    p->cursor_width = width;
    p->cursor_height = height;
    return glGetError() == GL_NO_ERROR ? 0 : fail(p, "native cursor upload failed");
}
void present_cursor_position(struct present *p, float x, float y, int visible, float hotspot_x,
                             float hotspot_y) {
    if (!p)
        return;
    p->cursor_x = x;
    p->cursor_y = y;
    p->cursor_visible = visible;
    p->hotspot_x = hotspot_x;
    p->hotspot_y = hotspot_y;
}
int present_fd(const struct present *p) {
    return p ? wl_display_get_fd(p->display) : -1;
}
int present_dispatch(struct present *p) {
    return p ? dispatch_wait(p, 0) : -1;
}
int present_valid(const struct present *p) {
    return p && p->valid && p->parent && p->parent->alive;
}
void present_free(struct present **state) {
    struct present *p = *state;
    if (!p)
        return;
    destroy_window(p);
    GLint current;
    glGetIntegerv(GL_CURRENT_PROGRAM, &current);
    if ((GLuint)current == p->program)
        glUseProgram(0);
    if (p->cursor)
        glDeleteTextures(1, &p->cursor);
    if (p->vertices)
        glDeleteBuffers(1, &p->vertices);
    if (p->program)
        glDeleteProgram(p->program);
    for (unsigned i = 0; i < p->surface_count; i++)
        if (p->surfaces[i].proxy)
            ivi_controller_surface_destroy(p->surfaces[i].proxy, 0);
    for (unsigned i = 0; i < p->layer_count; i++)
        if (p->layers[i].proxy)
            ivi_controller_layer_destroy(p->layers[i].proxy, 0);
    for (unsigned i = 0; i < p->screen_count; i++)
        ivi_controller_screen_destroy(p->screens[i]);
    for (unsigned i = 0; i < p->output_count; i++)
        wl_output_destroy(p->outputs[i]);
    if (p->controller)
        ivi_controller_destroy(p->controller);
    if (p->application)
        ivi_application_destroy(p->application);
    if (p->compositor)
        wl_compositor_destroy(p->compositor);
    if (p->registry)
        wl_registry_destroy(p->registry);
    wl_display_flush(p->display);
    free(p);
    *state = NULL;
}

/* SPDX-License-Identifier: GPL-3.0-or-later
 * A Wayland/IVI compositor stand-in with an existing renderer layer. The
 * renderer endpoint and its fixture surface share this process's PID.
 */
#define _GNU_SOURCE
#include "ivi-application-server-protocol.h"
#include "ivi-controller-server-protocol.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wayland-server.h>

#define MAX_OBJECTS 64
struct layer;
struct surface;
struct connection {
    struct wl_list link;
    struct wl_client *client;
    struct wl_resource *controller, *output;
    struct wl_listener destroyed;
};
struct reference {
    struct wl_list link;
    struct wl_resource *resource;
    void *object;
};
struct layer {
    uint32_t id;
    int alive, visible, rect[4];
    struct wl_list references;
    struct surface *members[MAX_OBJECTS];
    unsigned count;
};
struct surface {
    uint32_t id, pid;
    int alive, visible, width, height, destination[4];
    struct layer *parent;
    struct wl_list references, frames, pending_frames;
    struct wl_resource *resource, *buffer, *pending;
    struct wl_listener buffer_gone;
};
static struct wl_display *display;
static struct wl_list connections;
static struct layer layers[MAX_OBJECTS];
static struct surface surfaces[MAX_OBJECTS];
static unsigned layer_count, surface_count;
static int quitting, renderer_fd = -1;
static char renderer_path[108], snapshot_path[512];
static unsigned frame_count;
static struct wl_event_source *refresh_timer;
static uint64_t next_refresh;
#define REFRESH_NS (1000000000ull / 60)
static struct layer *fixture_layer;
static struct surface *fixture_surface;
static void stop(int signal_number) {
    (void)signal_number;
    quitting = 1;
}
static uint64_t now_ns(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}
static struct connection *connection_of(struct wl_client *client);
static void send_layer(struct wl_resource *resource, struct layer *layer);
static void send_surface(struct wl_resource *resource, struct surface *surface);
static struct wl_resource *resource_for(struct wl_list *references, struct wl_client *client) {
    struct reference *reference;
    wl_list_for_each(reference, references, link) if (wl_resource_get_client(reference->resource) ==
                                                      client) return reference->resource;
    return NULL;
}
static void reference_gone(struct wl_resource *resource) {
    struct reference *reference = wl_resource_get_user_data(resource);
    wl_list_remove(&reference->link);
    free(reference);
}
static void attach_reference(struct wl_resource *resource, struct wl_list *list, const void *implementation,
                             void *object) {
    struct reference *reference = calloc(1, sizeof *reference);
    reference->resource = resource;
    reference->object = object;
    wl_list_insert(list, &reference->link);
    wl_resource_set_implementation(resource, implementation, reference, reference_gone);
}
static void layer_notify(struct layer *layer) {
    struct reference *reference;
    wl_list_for_each(reference, &layer->references, link) send_layer(reference->resource, layer);
}
static void surface_notify(struct surface *surface) {
    struct reference *reference;
    wl_list_for_each(reference, &surface->references, link) send_surface(reference->resource, surface);
}
static void print_members(struct layer *layer) {
    printf("members %u", layer->id);
    for (unsigned i = 0; i < layer->count; i++)
        printf(" %u", layer->members[i]->id);
    printf("\n");
    fflush(stdout);
}
static void add_member(struct layer *layer, struct surface *surface) {
    for (unsigned i = 0; i < layer->count; i++)
        if (layer->members[i] == surface)
            return;
    if (layer->count < MAX_OBJECTS)
        layer->members[layer->count++] = surface;
    surface->parent = layer;
    surface_notify(surface);
    print_members(layer);
}
static void remove_member(struct layer *layer, struct surface *surface) {
    unsigned count = 0;
    for (unsigned i = 0; i < layer->count; i++)
        if (layer->members[i] != surface)
            layer->members[count++] = layer->members[i];
    layer->count = count;
    surface->parent = NULL;
    surface_notify(surface);
    print_members(layer);
}
static struct layer *new_layer(uint32_t id) {
    if (layer_count == MAX_OBJECTS)
        return NULL;
    struct layer *layer = &layers[layer_count++];
    layer->id = id;
    layer->alive = 1;
    layer->visible = 1;
    layer->rect[0] = -9373;
    layer->rect[1] = -18421;
    layer->rect[2] = 1920;
    layer->rect[3] = 720;
    wl_list_init(&layer->references);
    struct connection *connection;
    wl_list_for_each(connection, &connections, link) if (connection->controller)
        ivi_controller_send_layer(connection->controller, id);
    return layer;
}
static struct surface *new_surface(uint32_t id, uint32_t pid) {
    if (surface_count == MAX_OBJECTS)
        return NULL;
    struct surface *surface = &surfaces[surface_count++];
    surface->id = id;
    surface->pid = pid;
    surface->alive = 1;
    wl_list_init(&surface->references);
    wl_list_init(&surface->frames);
    wl_list_init(&surface->pending_frames);
    wl_list_init(&surface->buffer_gone.link);
    struct connection *connection;
    wl_list_for_each(connection, &connections, link) if (connection->controller && id)
        ivi_controller_send_surface(connection->controller, id);
    return surface;
}
static void destroy_scene_surface(struct surface *surface) {
    if (!surface->alive)
        return;
    if (surface->parent)
        remove_member(surface->parent, surface);
    surface->alive = 0;
    struct reference *reference;
    wl_list_for_each(reference, &surface->references, link)
        ivi_controller_surface_send_destroyed(reference->resource);
    printf("surface-gone %u\n", surface->id);
    fflush(stdout);
}
static void destroy_layer(struct layer *layer) {
    if (!layer->alive)
        return;
    layer->alive = 0;
    layer->visible = 0;
    while (layer->count)
        remove_member(layer, layer->members[layer->count - 1]);
    struct reference *reference;
    wl_list_for_each(reference, &layer->references, link)
        ivi_controller_layer_send_destroyed(reference->resource);
    printf("layer-gone %u\n", layer->id);
    fflush(stdout);
}
static void send_layer(struct wl_resource *resource, struct layer *layer) {
    ivi_controller_layer_send_visibility(resource, layer->visible);
    ivi_controller_layer_send_source_rectangle(resource, 0, 0, layer->rect[2], layer->rect[3]);
    ivi_controller_layer_send_destination_rectangle(resource, layer->rect[0], layer->rect[1], layer->rect[2],
                                                    layer->rect[3]);
    ivi_controller_layer_send_configuration(resource, layer->rect[2], layer->rect[3]);
    struct connection *connection = connection_of(wl_resource_get_client(resource));
    ivi_controller_layer_send_screen(resource, layer->alive ? connection->output : NULL);
}
static void send_surface(struct wl_resource *resource, struct surface *surface) {
    ivi_controller_surface_send_visibility(resource, surface->visible);
    ivi_controller_surface_send_source_rectangle(resource, 0, 0, surface->width, surface->height);
    ivi_controller_surface_send_destination_rectangle(resource, surface->destination[0],
                                                      surface->destination[1], surface->destination[2],
                                                      surface->destination[3]);
    ivi_controller_surface_send_configuration(resource, surface->width, surface->height);
    struct wl_resource *parent =
        surface->parent ? resource_for(&surface->parent->references, wl_resource_get_client(resource)) : NULL;
    ivi_controller_surface_send_layer(resource, parent);
}
static void resource_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static void ref_surface_visible(struct wl_client *client, struct wl_resource *resource, uint32_t value) {
    (void)client;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(resource))->object;
    if (surface == fixture_surface)
        printf("VIOLATION renderer surface visibility\n");
    surface->visible = value != 0;
    surface_notify(surface);
}
static void ref_surface_fixed(struct wl_client *client, struct wl_resource *resource, wl_fixed_t value) {
    (void)client;
    (void)resource;
    (void)value;
}
static void ref_surface_source(struct wl_client *client, struct wl_resource *resource, int32_t x, int32_t y,
                               int32_t w, int32_t h) {
    (void)client;
    (void)resource;
    (void)x;
    (void)y;
    (void)w;
    (void)h;
}
static void ref_surface_destination(struct wl_client *client, struct wl_resource *resource, int32_t x,
                                    int32_t y, int32_t w, int32_t h) {
    (void)client;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(resource))->object;
    surface->destination[0] = x;
    surface->destination[1] = y;
    surface->destination[2] = w;
    surface->destination[3] = h;
    surface_notify(surface);
}
static void ref_surface_size(struct wl_client *client, struct wl_resource *resource, int32_t w, int32_t h) {
    (void)client;
    (void)resource;
    (void)w;
    (void)h;
}
static void ref_surface_integer(struct wl_client *client, struct wl_resource *resource, int32_t value) {
    (void)client;
    (void)resource;
    (void)value;
}
static void ref_surface_screenshot(struct wl_client *client, struct wl_resource *resource, const char *path) {
    (void)client;
    (void)resource;
    (void)path;
}
static void ref_surface_stats(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(resource))->object;
    ivi_controller_surface_send_stats(resource, 0, 0, 0, surface->pid, "test-renderer");
}
static void ref_surface_destroy(struct wl_client *client, struct wl_resource *resource, int32_t scene) {
    (void)client;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(resource))->object;
    if (scene) {
        if (surface == fixture_surface)
            printf("VIOLATION renderer surface destroyed\n");
        destroy_scene_surface(surface);
    }
    wl_resource_destroy(resource);
}
static const struct ivi_controller_surface_interface surface_control_impl = {
    ref_surface_visible,     ref_surface_fixed, ref_surface_source,
    ref_surface_destination, ref_surface_size,  ref_surface_integer,
    ref_surface_screenshot,  ref_surface_stats, ref_surface_destroy};
static void ref_layer_visible(struct wl_client *client, struct wl_resource *resource, uint32_t value) {
    (void)client;
    (void)resource;
    (void)value;
    printf("VIOLATION parent layer visibility changed\n");
    fflush(stdout);
}
static void ref_layer_fixed(struct wl_client *client, struct wl_resource *resource, wl_fixed_t value) {
    (void)client;
    (void)resource;
    (void)value;
    printf("VIOLATION parent opacity changed\n");
}
static void ref_layer_rect(struct wl_client *client, struct wl_resource *resource, int32_t x, int32_t y,
                           int32_t w, int32_t h) {
    (void)client;
    (void)resource;
    (void)x;
    (void)y;
    (void)w;
    (void)h;
    printf("VIOLATION parent rectangle changed\n");
}
static void ref_layer_size(struct wl_client *client, struct wl_resource *resource, int32_t w, int32_t h) {
    (void)client;
    (void)resource;
    (void)w;
    (void)h;
}
static void ref_layer_integer(struct wl_client *client, struct wl_resource *resource, int32_t value) {
    (void)client;
    (void)resource;
    (void)value;
}
static void ref_layer_string(struct wl_client *client, struct wl_resource *resource, const char *path) {
    (void)client;
    (void)resource;
    (void)path;
}
static void ref_layer_clear(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    (void)resource;
    printf("VIOLATION parent surfaces cleared\n");
}
static void ref_layer_add(struct wl_client *client, struct wl_resource *resource, struct wl_resource *child) {
    (void)client;
    struct layer *layer = ((struct reference *)wl_resource_get_user_data(resource))->object;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(child))->object;
    add_member(layer, surface);
}
static void ref_layer_remove(struct wl_client *client, struct wl_resource *resource,
                             struct wl_resource *child) {
    (void)client;
    struct layer *layer = ((struct reference *)wl_resource_get_user_data(resource))->object;
    struct surface *surface = ((struct reference *)wl_resource_get_user_data(child))->object;
    remove_member(layer, surface);
}
static void ref_layer_order(struct wl_client *client, struct wl_resource *resource, struct wl_array *order) {
    (void)client;
    (void)resource;
    (void)order;
    printf("VIOLATION parent render order changed\n");
}
static void ref_layer_destroy(struct wl_client *client, struct wl_resource *resource, int32_t scene) {
    (void)client;
    struct layer *layer = ((struct reference *)wl_resource_get_user_data(resource))->object;
    if (scene) {
        printf("VIOLATION parent layer destroyed\n");
        destroy_layer(layer);
    }
    wl_resource_destroy(resource);
}
static const struct ivi_controller_layer_interface layer_control_impl = {
    ref_layer_visible, ref_layer_fixed, ref_layer_rect, ref_layer_rect,   ref_layer_size,  ref_layer_integer,
    ref_layer_string,  ref_layer_clear, ref_layer_add,  ref_layer_remove, ref_layer_order, ref_layer_destroy};
static void screen_none(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    (void)resource;
    printf("VIOLATION screen render order\n");
}
static void screen_add(struct wl_client *client, struct wl_resource *resource, struct wl_resource *layer) {
    (void)client;
    (void)resource;
    (void)layer;
    printf("VIOLATION screen layer added\n");
}
static void screen_shot(struct wl_client *client, struct wl_resource *resource, const char *path) {
    (void)client;
    (void)resource;
    (void)path;
}
static void screen_order(struct wl_client *client, struct wl_resource *resource, struct wl_array *order) {
    (void)client;
    (void)resource;
    (void)order;
    printf("VIOLATION screen render order\n");
}
static const struct ivi_controller_screen_interface screen_impl = {resource_destroy, screen_none, screen_add,
                                                                   screen_shot, screen_order};
static void controller_commit(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    (void)resource;
}
static void controller_layer_create(struct wl_client *client, struct wl_resource *resource, uint32_t id,
                                    int32_t width, int32_t height, uint32_t new_id) {
    (void)resource;
    (void)width;
    (void)height;
    struct layer *layer = NULL;
    for (unsigned i = 0; i < layer_count; i++)
        if (layers[i].alive && layers[i].id == id)
            layer = &layers[i];
    if (!layer) {
        printf("VIOLATION new layer %u\n", id);
        fflush(stdout);
        return;
    }
    struct wl_resource *proxy = wl_resource_create(client, &ivi_controller_layer_interface, 1, new_id);
    attach_reference(proxy, &layer->references, &layer_control_impl, layer);
    send_layer(proxy, layer);
}
static void controller_surface_create(struct wl_client *client, struct wl_resource *resource, uint32_t id,
                                      uint32_t new_id) {
    (void)resource;
    struct surface *surface = NULL;
    for (unsigned i = 0; i < surface_count; i++)
        if (surfaces[i].alive && surfaces[i].id == id)
            surface = &surfaces[i];
    if (!surface)
        return;
    struct wl_resource *proxy = wl_resource_create(client, &ivi_controller_surface_interface, 1, new_id);
    attach_reference(proxy, &surface->references, &surface_control_impl, surface);
    send_surface(proxy, surface);
}
static const struct ivi_controller_interface controller_impl = {controller_commit, controller_layer_create,
                                                                controller_surface_create};
static void connection_gone(struct wl_listener *listener, void *data) {
    (void)data;
    struct connection *connection = wl_container_of(listener, connection, destroyed);
    wl_list_remove(&connection->link);
    free(connection);
}
static struct connection *connection_of(struct wl_client *client) {
    struct connection *connection;
    wl_list_for_each(connection, &connections, link) if (connection->client == client) return connection;
    connection = calloc(1, sizeof *connection);
    connection->client = client;
    connection->destroyed.notify = connection_gone;
    wl_client_add_destroy_listener(client, &connection->destroyed);
    wl_list_insert(&connections, &connection->link);
    return connection;
}
static void bind_controller(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct connection *connection = connection_of(client);
    connection->controller = wl_resource_create(client, &ivi_controller_interface, version, id);
    wl_resource_set_implementation(connection->controller, &controller_impl, NULL, NULL);
    struct wl_resource *screen = wl_resource_create(client, &ivi_controller_screen_interface, 1, 0);
    wl_resource_set_implementation(screen, &screen_impl, NULL, NULL);
    ivi_controller_send_screen(connection->controller, 91, screen);
    for (unsigned i = 0; i < layer_count; i++)
        if (layers[i].alive)
            ivi_controller_send_layer(connection->controller, layers[i].id);
    for (unsigned i = 0; i < surface_count; i++)
        if (surfaces[i].alive && surfaces[i].id)
            ivi_controller_send_surface(connection->controller, surfaces[i].id);
}
static void output_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}
static const struct wl_output_interface output_impl = {output_release};
static void bind_output(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct connection *connection = connection_of(client);
    connection->output = wl_resource_create(client, &wl_output_interface, version, id);
    wl_resource_set_implementation(connection->output, &output_impl, NULL, NULL);
    wl_output_send_geometry(connection->output, 0, 0, 1920, 720, 0, "test", "test", 0);
    wl_output_send_mode(connection->output, WL_OUTPUT_MODE_CURRENT, 1920, 720, 60000);
}
static void buffer_destroyed(struct wl_listener *listener, void *data) {
    (void)data;
    struct surface *surface = wl_container_of(listener, surface, buffer_gone);
    surface->buffer = NULL;
    wl_list_remove(&surface->buffer_gone.link);
    wl_list_init(&surface->buffer_gone.link);
}
static void callback_gone(struct wl_resource *resource) {
    wl_list_remove(wl_resource_get_link(resource));
}
static int refresh(void *data) {
    (void)data;
    uint64_t now = now_ns();
    /* Frame callbacks pace the client against output refreshes; adding 16 ms
     * after each commit would incorrectly add client draw time to every period. */
    for (unsigned i = 0; i < surface_count; i++) {
        struct surface *surface = &surfaces[i];
        if (!surface->alive || !surface->visible || !surface->parent || !surface->parent->visible)
            continue;
        struct wl_resource *callback, *temporary;
        wl_resource_for_each_safe(callback, temporary, &surface->frames) {
            wl_callback_send_done(callback, (uint32_t)(now / 1000000));
            wl_resource_destroy(callback);
        }
    }
    do
        next_refresh += REFRESH_NS;
    while (next_refresh <= now);
    wl_event_source_timer_update(refresh_timer, (int)((next_refresh - now + 999999) / 1000000));
    return 0;
}
static void surface_attach(struct wl_client *client, struct wl_resource *resource, struct wl_resource *buffer,
                           int32_t x, int32_t y) {
    (void)client;
    (void)x;
    (void)y;
    ((struct surface *)wl_resource_get_user_data(resource))->pending = buffer;
}
static void surface_damage(struct wl_client *client, struct wl_resource *resource, int32_t x, int32_t y,
                           int32_t width, int32_t height) {
    (void)client;
    (void)resource;
    (void)x;
    (void)y;
    (void)width;
    (void)height;
}
static void surface_frame(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    struct surface *surface = wl_resource_get_user_data(resource);
    struct wl_resource *callback = wl_resource_create(client, &wl_callback_interface, 1, id);
    wl_resource_set_implementation(callback, NULL, NULL, callback_gone);
    wl_list_insert(surface->pending_frames.prev, wl_resource_get_link(callback));
}
static void surface_region(struct wl_client *client, struct wl_resource *resource,
                           struct wl_resource *region) {
    (void)client;
    (void)resource;
    (void)region;
}
static void surface_input_region(struct wl_client *client, struct wl_resource *resource,
                                 struct wl_resource *region) {
    (void)client;
    struct surface *surface = wl_resource_get_user_data(resource);
    if (!region || !wl_resource_get_user_data(region))
        printf("VIOLATION native input region is not empty\n");
    else
        printf("input-empty %u\n", surface->id);
    fflush(stdout);
}
static void surface_commit(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct surface *surface = wl_resource_get_user_data(resource);
    if (surface->pending) {
        if (surface->buffer) {
            wl_list_remove(&surface->buffer_gone.link);
            wl_list_init(&surface->buffer_gone.link);
            wl_buffer_send_release(surface->buffer);
        }
        surface->buffer = surface->pending;
        surface->pending = NULL;
        surface->buffer_gone.notify = buffer_destroyed;
        wl_resource_add_destroy_listener(surface->buffer, &surface->buffer_gone);
        struct wl_shm_buffer *buffer = wl_shm_buffer_get(surface->buffer);
        if (buffer) {
            int width = wl_shm_buffer_get_width(buffer), height = wl_shm_buffer_get_height(buffer),
                stride = wl_shm_buffer_get_stride(buffer);
            if (snapshot_path[0]) {
                wl_shm_buffer_begin_access(buffer);
                FILE *file = fopen(snapshot_path, "wb");
                if (file) {
                    fprintf(file,
                            "P7\nWIDTH %d\nHEIGHT %d\nDEPTH 4\nMAXVAL 255\nTUPLTYPE "
                            "RGB_ALPHA\nENDHDR\n",
                            width, height);
                    const uint8_t *pixels = wl_shm_buffer_get_data(buffer);
                    uint8_t *row = malloc((size_t)width * 4);
                    if (row) {
                        for (int y = 0; y < height; y++) {
                            for (int x = 0; x < width; x++) {
                                const uint8_t *pixel = pixels + y * stride + x * 4;
                                row[x * 4] = pixel[2];
                                row[x * 4 + 1] = pixel[1];
                                row[x * 4 + 2] = pixel[0];
                                row[x * 4 + 3] = pixel[3];
                            }
                            fwrite(row, (size_t)width * 4, 1, file);
                        }
                        free(row);
                    }
                    fclose(file);
                }
                wl_shm_buffer_end_access(buffer);
            }
            printf("commit %u %u %d %d\n", ++frame_count, surface->id, width, height);
            fflush(stdout);
        }
    }
    /* wl_surface.frame requests take effect on the following commit. */
    if (!wl_list_empty(&surface->pending_frames)) {
        wl_list_insert_list(surface->frames.prev, &surface->pending_frames);
        wl_list_init(&surface->pending_frames);
    }
}
static void surface_integer_request(struct wl_client *client, struct wl_resource *resource, int32_t value) {
    (void)client;
    (void)resource;
    (void)value;
}
static const struct wl_surface_interface surface_impl = {.destroy = resource_destroy,
                                                         .attach = surface_attach,
                                                         .damage = surface_damage,
                                                         .frame = surface_frame,
                                                         .set_opaque_region = surface_region,
                                                         .set_input_region = surface_input_region,
                                                         .commit = surface_commit,
                                                         .set_buffer_transform = surface_integer_request,
                                                         .set_buffer_scale = surface_integer_request,
                                                         .damage_buffer = surface_damage};
static void surface_gone(struct wl_resource *resource) {
    struct surface *surface = wl_resource_get_user_data(resource);
    destroy_scene_surface(surface);
    if (surface->buffer) {
        wl_list_remove(&surface->buffer_gone.link);
        wl_buffer_send_release(surface->buffer);
        surface->buffer = NULL;
    }
    struct wl_resource *callback, *temporary;
    wl_resource_for_each_safe(callback, temporary, &surface->frames) wl_resource_destroy(callback);
    wl_resource_for_each_safe(callback, temporary, &surface->pending_frames) wl_resource_destroy(callback);
}
static void compositor_surface(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    (void)resource;
    pid_t pid;
    uid_t uid;
    gid_t gid;
    wl_client_get_credentials(client, &pid, &uid, &gid);
    struct surface *surface = new_surface(0, (uint32_t)pid);
    surface->resource = wl_resource_create(client, &wl_surface_interface, 1, id);
    wl_resource_set_implementation(surface->resource, &surface_impl, surface, surface_gone);
}
static void region_rectangle(struct wl_client *client, struct wl_resource *resource, int32_t x, int32_t y,
                             int32_t width, int32_t height) {
    (void)client;
    wl_resource_set_user_data(resource, NULL);
    (void)x;
    (void)y;
    (void)width;
    (void)height;
}
static const struct wl_region_interface region_impl = {resource_destroy, region_rectangle, region_rectangle};
static void compositor_region(struct wl_client *client, struct wl_resource *resource, uint32_t id) {
    (void)resource;
    struct wl_resource *region = wl_resource_create(client, &wl_region_interface, 1, id);
    wl_resource_set_implementation(region, &region_impl, (void *)1, NULL);
}
static const struct wl_compositor_interface compositor_impl = {compositor_surface, compositor_region};
static void bind_compositor(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct wl_resource *resource = wl_resource_create(client, &wl_compositor_interface, version, id);
    wl_resource_set_implementation(resource, &compositor_impl, NULL, NULL);
}
static const struct ivi_surface_interface ivi_surface_impl = {resource_destroy};
static void application_surface(struct wl_client *client, struct wl_resource *resource, uint32_t ivi_id,
                                struct wl_resource *wl_surface, uint32_t id) {
    (void)resource;
    struct surface *surface = wl_resource_get_user_data(wl_surface);
    surface->id = ivi_id;
    struct wl_resource *ivi = wl_resource_create(client, &ivi_surface_interface, 1, id);
    wl_resource_set_implementation(ivi, &ivi_surface_impl, NULL, NULL);
    struct connection *connection;
    wl_list_for_each(connection, &connections, link) if (connection->controller)
        ivi_controller_send_surface(connection->controller, ivi_id);
}
static const struct ivi_application_interface application_impl = {application_surface};
static void bind_application(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct wl_resource *resource = wl_resource_create(client, &ivi_application_interface, version, id);
    wl_resource_set_implementation(resource, &application_impl, NULL, NULL);
}
static void command(const char *text) {
    int x, y, w, h, value;
    unsigned reserved;
    if (sscanf(text, "rect %d %d %d %d", &x, &y, &w, &h) == 4) {
        fixture_layer->rect[0] = x;
        fixture_layer->rect[1] = y;
        fixture_layer->rect[2] = w;
        fixture_layer->rect[3] = h;
        layer_notify(fixture_layer);
    } else if (sscanf(text, "visible %d", &value) == 1) {
        fixture_layer->visible = value != 0;
        layer_notify(fixture_layer);
        printf("visible %d\n", value);
    } else if (sscanf(text, "pid %d", &value) == 1)
        fixture_surface->pid = (uint32_t)value;
    else if (sscanf(text, "reserve %u", &reserved) == 1) {
        struct surface *surface = new_surface(reserved, (uint32_t)getpid());
        add_member(fixture_layer, surface);
    } else if (!strncmp(text, "destroy", 7))
        destroy_layer(fixture_layer);
    else if (!strncmp(text, "replace", 7)) {
        destroy_layer(fixture_layer);
        fixture_layer = new_layer(1097 + layer_count);
        fixture_surface = new_surface(4007 + surface_count, (uint32_t)getpid());
        add_member(fixture_layer, fixture_surface);
    } else if (!strncmp(text, "ambiguous", 9)) {
        struct layer *layer = new_layer(2011 + layer_count);
        struct surface *surface = new_surface(7001 + surface_count, (uint32_t)getpid());
        add_member(layer, surface);
    } else if (!strncmp(text, "quit", 4))
        quitting = 1;
    printf("command %s\n", text);
    fflush(stdout);
}
static int input(int fd, uint32_t mask, void *data) {
    (void)mask;
    (void)data;
    static char buffer[1024];
    static size_t used;
    ssize_t count = read(fd, buffer + used, sizeof buffer - used - 1);
    if (count <= 0)
        return 0;
    used += (size_t)count;
    buffer[used] = 0;
    char *start = buffer, *end;
    while ((end = strchr(start, '\n'))) {
        *end = 0;
        command(start);
        start = end + 1;
    }
    used -= start - buffer;
    memmove(buffer, start, used);
    return 0;
}
static int renderer_accept(int fd, uint32_t mask, void *data) {
    (void)mask;
    (void)data;
    int client = accept(fd, NULL, NULL);
    if (client >= 0)
        close(client);
    return 0;
}
int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: mock_presenter WAYLAND_SOCKET RENDER_SOCKET SNAPSHOT\n");
        return 2;
    }
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    wl_list_init(&connections);
    display = wl_display_create();
    fixture_layer = new_layer(1097);
    fixture_surface = new_surface(4007, (uint32_t)getpid());
    add_member(fixture_layer, fixture_surface);
    if (wl_display_add_socket(display, argv[1]) < 0)
        return 1;
    wl_display_init_shm(display);
    wl_global_create(display, &wl_compositor_interface, 1, NULL, bind_compositor);
    wl_global_create(display, &ivi_application_interface, 1, NULL, bind_application);
    /* Announce controller first, as production compositors may do. */
    wl_global_create(display, &ivi_controller_interface, 1, NULL, bind_controller);
    wl_global_create(display, &wl_output_interface, 1, NULL, bind_output);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(argv[2]) >= sizeof address.sun_path)
        return 1;
    strcpy(address.sun_path, argv[2]);
    strcpy(renderer_path, argv[2]);
    const char *activated = getenv("TEST_RENDERER_FD");
    if (activated) renderer_fd = atoi(activated);
    else {
        renderer_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (renderer_fd < 0 || bind(renderer_fd, (struct sockaddr *)&address, sizeof address) ||
            listen(renderer_fd, 4))
            return 1;
    }
    if (strcmp(argv[3], "/dev/null"))
        snprintf(snapshot_path, sizeof snapshot_path, "%s", argv[3]);
    struct wl_event_loop *loop = wl_display_get_event_loop(display);
    refresh_timer = wl_event_loop_add_timer(loop, refresh, NULL);
    next_refresh = now_ns();
    wl_event_source_timer_update(refresh_timer, 1);
    wl_event_loop_add_fd(loop, STDIN_FILENO, WL_EVENT_READABLE, input, NULL);
    wl_event_loop_add_fd(loop, renderer_fd, WL_EVENT_READABLE, renderer_accept, NULL);
    printf("ready %u\n", (unsigned)getpid());
    fflush(stdout);
    while (!quitting) {
        wl_display_flush_clients(display);
        wl_event_loop_dispatch(loop, 50);
    }
    wl_display_destroy(display);
    close(renderer_fd);
    unlink(renderer_path);
    return 0;
}

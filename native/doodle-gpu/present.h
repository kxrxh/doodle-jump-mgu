/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef DOODLE_PRESENT_H
#define DOODLE_PRESENT_H
#include <EGL/egl.h>
#include <stdint.h>
struct wl_display;
struct present;
typedef void (*present_done)(void *data, uint32_t timestamp, int presented);
struct present *present_create(struct wl_display *display, EGLDisplay egl, EGLContext context,
                               EGLConfig config, EGLSurface idle);
int present_bind(struct present *present, const char *renderer_socket, const int rectangle[4]);
int present_configure(struct present *present, int x, int y, int width, int height, int show);
int present_draw(struct present *present, unsigned texture, present_done done, void *data);
void present_hide(struct present *present);
int present_fd(const struct present *present);
int present_dispatch(struct present *present);
int present_valid(const struct present *present);
int present_cursor_image(struct present *present, unsigned width, unsigned height, const uint8_t *rgba);
void present_cursor_position(struct present *present, float x, float y, int visible, float hotspot_x,
                             float hotspot_y);
const char *present_error(const struct present *present);
void present_free(struct present **present);
#endif

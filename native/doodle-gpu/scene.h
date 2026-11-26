/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef MGU_SCENE_H
#define MGU_SCENE_H
#include <stddef.h>
#include <stdint.h>

struct scene;
int scene_init(void);
int scene_supported(void);
int scene_texture(struct scene **scene, const uint8_t *payload, size_t bytes);
int scene_frame(struct scene **scene, const uint8_t *payload, size_t bytes);
unsigned scene_render(struct scene *scene);
void scene_free(struct scene **scene);
const char *scene_error(void);
#endif

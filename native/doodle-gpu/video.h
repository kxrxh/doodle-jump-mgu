/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef DOODLE_VIDEO_H
#define DOODLE_VIDEO_H
#include <stdint.h>

struct video;
struct video *video_create(unsigned width, unsigned height);
int video_publish(struct video *video, unsigned texture);
const char *video_path(const struct video *video);
uint32_t video_sequence(const struct video *video);
void video_free(struct video **video);
const char *video_error(void);
#endif

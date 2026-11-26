/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <GLES2/gl2.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include "video.h"

#define VIDEO_SLOTS 3
struct video {
    char path[64];
    unsigned width, height;
    size_t frame_bytes, mapped_bytes;
    uint32_t *header;
    GLuint program, framebuffer, texture, vertices;
    GLint source_uniform, size_uniform;
};
static char error[256];
const char *video_error(void) { return error; }
static int fail(const char *message) {
    snprintf(error, sizeof error, "video: %s", message);
    return -1;
}
static GLuint shader(GLenum kind, const char *source) {
    GLuint value = glCreateShader(kind);
    GLint valid;
    glShaderSource(value, 1, &source, NULL);
    glCompileShader(value);
    glGetShaderiv(value, GL_COMPILE_STATUS, &valid);
    if (!valid) {
        glGetShaderInfoLog(value, sizeof error, NULL, error);
        glDeleteShader(value);
        return 0;
    }
    return value;
}
static const char *vertex_source =
    "attribute vec2 position;\n"
    "void main(){gl_Position=vec4(position,0.0,1.0);}\n";
/* Each RGBA output pixel holds four consecutive bytes of the planar I420 frame.
   The packed target's first GL row contains the top of the game's Y plane. */
static const char *fragment_source =
    "precision highp float;\n"
    "uniform sampler2D source; uniform vec2 size;\n"
    "vec3 rgb(vec2 p){return texture2D(source,vec2((p.x+0.5)/size.x,1.0-(p.y+0.5)/size.y)).rgb;}\n"
    "float component(float index){\n"
    " float pixels=size.x*size.y;\n"
    " if(index<pixels){return dot(rgb(vec2(mod(index,size.x),floor(index/size.x))),vec3(0.299,0.587,0.114));}\n"
    " float chroma=index-pixels; bool v=chroma>=pixels*0.25;\n"
    " if(v)chroma-=pixels*0.25;\n"
    " vec2 p=vec2(mod(chroma,size.x*0.5),floor(chroma/(size.x*0.5)))*2.0;\n"
    " vec3 c=(rgb(p)+rgb(p+vec2(1.0,0.0))+rgb(p+vec2(0.0,1.0))+rgb(p+vec2(1.0,1.0)))*0.25;\n"
    " return dot(c,v?vec3(0.5,-0.418688,-0.081312):vec3(-0.168736,-0.331264,0.5))+0.5019607843;\n"
    "}\n"
    "void main(){float i=floor(gl_FragCoord.y)*size.x+floor(gl_FragCoord.x)*4.0;\n"
    "gl_FragColor=vec4(component(i),component(i+1.0),component(i+2.0),component(i+3.0));}\n";

static void remove_abandoned_frames(void) {
    DIR *directory = opendir("/dev/shm");
    if (!directory) return;
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        static const char prefix[] = "mgu-video-doodle-";
        if (strncmp(entry->d_name, prefix, sizeof prefix - 1)) continue;
        char *suffix;
        long pid = strtol(entry->d_name + sizeof prefix - 1, &suffix, 10);
        if (pid <= 0 || pid > 0x7fffffff || *suffix != '-' || !suffix[1]) continue;
        if (kill((pid_t)pid, 0) == 0 || errno != ESRCH) continue;
        struct stat status;
        if (fstatat(dirfd(directory), entry->d_name, &status, AT_SYMLINK_NOFOLLOW) == 0 &&
            S_ISREG(status.st_mode) && status.st_uid == getuid()) unlinkat(dirfd(directory), entry->d_name, 0);
    }
    closedir(directory);
}
struct video *video_create(unsigned width, unsigned height) {
    if (width < 16 || height < 16 || width > 2048 || height > 2048 || width % 8 || height % 2) {
        fail("invalid output dimensions");
        return NULL;
    }
    struct video *v = calloc(1, sizeof *v);
    if (!v) { fail("allocation failed"); return NULL; }
    v->width = width;
    v->height = height;
    v->frame_bytes = (size_t)width * height * 3 / 2;
    v->mapped_bytes = 64 + VIDEO_SLOTS * v->frame_bytes;
    remove_abandoned_frames();
    snprintf(v->path, sizeof v->path, "/dev/shm/mgu-video-doodle-%ld-XXXXXX", (long)getpid());
    int fd = mkstemp(v->path);
    if (fd < 0) { fail("cannot create frame file"); goto failed; }
    if (fchmod(fd, 0644) < 0 || ftruncate(fd, (off_t)v->mapped_bytes) < 0) {
        close(fd); fail("cannot size frame file"); goto failed;
    }
    void *memory = mmap(NULL, v->mapped_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (memory == MAP_FAILED) { fail("cannot map frame file"); goto failed; }
    v->header = memory;
    v->header[0] = 0x4656474d;
    v->header[1] = 1;
    v->header[2] = width;
    v->header[3] = height;
    v->header[6] = VIDEO_SLOTS;
    v->header[8] = 1;
    GLuint vertex = shader(GL_VERTEX_SHADER, vertex_source);
    GLuint fragment = shader(GL_FRAGMENT_SHADER, fragment_source);
    if (!vertex || !fragment) {
        if (vertex) glDeleteShader(vertex);
        if (fragment) glDeleteShader(fragment);
        goto failed;
    }
    v->program = glCreateProgram();
    glAttachShader(v->program, vertex);
    glAttachShader(v->program, fragment);
    glBindAttribLocation(v->program, 0, "position");
    glLinkProgram(v->program);
    glDeleteShader(vertex);
    glDeleteShader(fragment);
    GLint linked;
    glGetProgramiv(v->program, GL_LINK_STATUS, &linked);
    if (!linked) { glGetProgramInfoLog(v->program, sizeof error, NULL, error); goto failed; }
    v->source_uniform = glGetUniformLocation(v->program, "source");
    v->size_uniform = glGetUniformLocation(v->program, "size");
    glGenTextures(1, &v->texture);
    glBindTexture(GL_TEXTURE_2D, v->texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, (GLsizei)(width / 4), (GLsizei)(height * 3 / 2),
                 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &v->framebuffer);
    glBindFramebuffer(GL_FRAMEBUFFER, v->framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, v->texture, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        fail("packed framebuffer allocation failed"); goto failed;
    }
    static const GLfloat vertices[] = {-1,-1, 1,-1, -1,1, 1,1};
    glGenBuffers(1, &v->vertices);
    glBindBuffer(GL_ARRAY_BUFFER, v->vertices);
    glBufferData(GL_ARRAY_BUFFER, sizeof vertices, vertices, GL_STATIC_DRAW);
    if (glGetError() != GL_NO_ERROR) { fail("GPU allocation failed"); goto failed; }
    return v;
failed:
    video_free(&v);
    return NULL;
}
int video_publish(struct video *v, unsigned texture) {
    if (!v || !texture) return fail("missing scene texture");
    for (int i = 0; i < 8 && glGetError() != GL_NO_ERROR; i++) {}
    glBindFramebuffer(GL_FRAMEBUFFER, v->framebuffer);
    glViewport(0, 0, (GLsizei)(v->width / 4), (GLsizei)(v->height * 3 / 2));
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_DITHER);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glUseProgram(v->program);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glUniform1i(v->source_uniform, 0);
    glUniform2f(v->size_uniform, (float)v->width, (float)v->height);
    glBindBuffer(GL_ARRAY_BUFFER, v->vertices);
    glEnableVertexAttribArray(0);
    glDisableVertexAttribArray(1);
    glDisableVertexAttribArray(2);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    uint32_t slot = (v->header[5] + 1) % VIDEO_SLOTS;
    void *target = (uint8_t *)v->header + 64 + slot * v->frame_bytes;
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, (GLsizei)(v->width / 4), (GLsizei)(v->height * 3 / 2), GL_RGBA, GL_UNSIGNED_BYTE, target);
    if (glGetError() != GL_NO_ERROR) return fail("GPU readback failed");
    uint32_t sequence = v->header[4] + 1;
    if (!sequence) sequence = 1;
    __atomic_store_n(&v->header[5], slot, __ATOMIC_RELEASE);
    __atomic_store_n(&v->header[4], sequence, __ATOMIC_RELEASE);
    return 0;
}
const char *video_path(const struct video *v) { return v->path; }
void video_resume_sequence(struct video *v, uint32_t sequence) { v->header[4] = sequence; }
uint32_t video_sequence(const struct video *v) { return v->header[4]; }
void video_free(struct video **state) {
    struct video *v = *state;
    if (!v) return;
    if (v->header) munmap(v->header, v->mapped_bytes);
    if (v->path[0]) unlink(v->path);
    if (v->vertices) glDeleteBuffers(1, &v->vertices);
    if (v->framebuffer) glDeleteFramebuffers(1, &v->framebuffer);
    if (v->texture) glDeleteTextures(1, &v->texture);
    if (v->program) {
        GLint current;
        glGetIntegerv(GL_CURRENT_PROGRAM, &current);
        // A bound deleted program otherwise survives until the next scene tries to restore it.
        if ((GLuint)current == v->program) glUseProgram(0);
        glDeleteProgram(v->program);
    }
    free(v);
    *state = NULL;
}

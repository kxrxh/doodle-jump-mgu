/* SPDX-License-Identifier: GPL-3.0-or-later */
/* GLES 1 game draw lists executed in a private GLES 2 framebuffer. */
#include <GLES2/gl2.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "scene.h"

#define SCENE_SIDE 2048
#define SCENE_TEXTURE_BYTES (64u << 20)
#define SCENE_ALL_BYTES (128u << 20)
#define SCENE_COMMAND_BYTES (24u << 20)
#define SCENE_TEXTURES 4096

struct texture { uint32_t id; GLuint tex; int w, h; struct texture *next; };
struct scene {
    struct texture *textures;
    size_t texture_bytes, command_bytes;
    unsigned ntexture;
    uint32_t *commands;
    int w, h, fw, fh, dirty;
    GLuint tex, fbo, vbo;
};
static size_t all_bytes;
static GLuint program;
static GLint u_size, u_texture, u_sample, u_replace, u_flags, u_tex_size;
static char error[256];
const char *scene_error(void) { return error; }
int scene_supported(void) { return program!=0; }
static int fail(const char *message) { snprintf(error, sizeof error, "scene: %s", message); return -1; }
static int gpu_fail(const char *message) { fail(message); return -2; }
static uint32_t word(const uint8_t *p, size_t n) { uint32_t v; memcpy(&v, p+n*4, 4); return v; }
static float number(const uint32_t *p) { float v; memcpy(&v, p, 4); return v; }
static struct texture *find(struct scene *s, uint32_t id) {
    for (struct texture *t = s ? s->textures : NULL; t; t=t->next) if (t->id==id) return t;
    return NULL;
}
static struct scene *get(struct scene **s) { if (!*s) *s=calloc(1,sizeof **s); return *s; }

static const char *vs_source =
    "attribute vec2 a_pos; attribute vec2 a_uv; attribute vec4 a_color;\n"
    "uniform vec2 u_size; varying vec2 uv; varying vec4 color;\n"
    "void main(){ gl_Position=vec4(a_pos.x/u_size.x*2.0-1.0,1.0-a_pos.y/u_size.y*2.0,0.0,1.0); uv=a_uv; color=a_color; }\n";
static const char *fs_source =
    "precision highp float; varying vec2 uv; varying vec4 color;\n"
    "uniform sampler2D u_texture; uniform vec2 u_tex_size; uniform int u_sample; uniform int u_replace; uniform int u_flags;\n"
    /* Sampling at texel centres avoids the GLES2 restriction on repeating NPOT textures. */
    "vec4 texel(vec2 p){\n"
    " p.x=(u_flags==4||u_flags==6||u_flags==12||u_flags==14)?mod(p.x,u_tex_size.x):clamp(p.x,0.0,u_tex_size.x-1.0);\n"
    " p.y=(u_flags>=8)?mod(p.y,u_tex_size.y):clamp(p.y,0.0,u_tex_size.y-1.0);\n"
    " return texture2D(u_texture,(p+0.5)/u_tex_size); }\n"
    "void main(){ vec4 t=vec4(1.0);\n"
    " if(u_sample!=0){\n"
    "  vec2 p=uv*u_tex_size;\n"
    "  if(u_flags==2||u_flags==6||u_flags==10||u_flags==14){\n"
    "   p-=0.5; vec2 b=floor(p), f=p-b;\n"
    "   vec4 top=texel(b)*(1.0-f.x)+texel(b+vec2(1.0,0.0))*f.x;\n"
    "   vec4 bottom=texel(b+vec2(0.0,1.0))*(1.0-f.x)+texel(b+vec2(1.0,1.0))*f.x;\n"
    "   t=top*(1.0-f.y)+bottom*f.y;\n"
    "  }else t=texel(floor(p));\n"
    " }\n"
    " if(u_sample==0||u_replace==0) t*=color;\n"
    " else if(u_replace==2) t.rgb=color.rgb*color.a+t.rgb*(1.0-color.a);\n"
    " gl_FragColor=t; }\n";

static GLuint shader(GLenum kind, const char *source) {
    GLuint s=glCreateShader(kind); GLint ok;
    glShaderSource(s,1,&source,NULL); glCompileShader(s); glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
    if (!ok) { glGetShaderInfoLog(s,sizeof error,NULL,error); glDeleteShader(s); return 0; }
    return s;
}
int scene_init(void) {
    GLuint vs=shader(GL_VERTEX_SHADER,vs_source), fs=shader(GL_FRAGMENT_SHADER,fs_source);
    if (!vs || !fs) { if(vs)glDeleteShader(vs); if(fs)glDeleteShader(fs); return -1; }
    GLuint p=glCreateProgram(); GLint ok;
    glAttachShader(p,vs); glAttachShader(p,fs);
    glBindAttribLocation(p,0,"a_pos"); glBindAttribLocation(p,1,"a_uv"); glBindAttribLocation(p,2,"a_color");
    glLinkProgram(p); glDeleteShader(vs); glDeleteShader(fs); glGetProgramiv(p,GL_LINK_STATUS,&ok);
    if (!ok) { glGetProgramInfoLog(p,sizeof error,NULL,error); glDeleteProgram(p); return -1; }
    program=p;
    u_size=glGetUniformLocation(p,"u_size"); u_texture=glGetUniformLocation(p,"u_texture");
    u_sample=glGetUniformLocation(p,"u_sample"); u_replace=glGetUniformLocation(p,"u_replace");
    u_flags=glGetUniformLocation(p,"u_flags"); u_tex_size=glGetUniformLocation(p,"u_tex_size");
    return 0;
}

int scene_texture(struct scene **state, const uint8_t *p, size_t bytes) {
    if (!scene_supported()) return gpu_fail("GPU scenes unavailable");
    if (!p || bytes<12 || !word(p,0)) return fail("invalid texture header");
    uint32_t id=word(p,0), w=word(p,1), h=word(p,2);
    if ((!w)!=(!h) || w>SCENE_SIDE || h>SCENE_SIDE || bytes!=12+(size_t)w*h*4) return fail("invalid texture size");
    struct scene *s=get(state);
    if (!s) return gpu_fail("allocation failed");
    struct texture *old=find(s,id);
    size_t before=old ? (size_t)old->w*old->h*4 : 0, after=(size_t)w*h*4;
    if (!w) {
        if (old) {
            struct texture **next=&s->textures;
            while (*next!=old) next=&(*next)->next;
            *next=old->next; glDeleteTextures(1,&old->tex); free(old);
            s->texture_bytes-=before; all_bytes-=before; s->ntexture--;
        }
        return 0;
    }
    if (s->texture_bytes-before+after>SCENE_TEXTURE_BYTES || all_bytes-before+after>SCENE_ALL_BYTES ||
        (!old && s->ntexture>=SCENE_TEXTURES)) return gpu_fail("texture memory limit");
    struct texture *t=calloc(1,sizeof *t);
    if (!t) return gpu_fail("allocation failed");
    GLint binding, alignment;
    glGetIntegerv(GL_TEXTURE_BINDING_2D,&binding); glGetIntegerv(GL_UNPACK_ALIGNMENT,&alignment);
    for (int i=0;i<8 && glGetError()!=GL_NO_ERROR;i++) {}
    glGenTextures(1,&t->tex); glBindTexture(GL_TEXTURE_2D,t->tex);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT,1);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,(int)w,(int)h,0,GL_RGBA,GL_UNSIGNED_BYTE,p+12);
    GLenum e=glGetError();
    glPixelStorei(GL_UNPACK_ALIGNMENT,alignment); glBindTexture(GL_TEXTURE_2D,(GLuint)binding);
    if (e!=GL_NO_ERROR) { glDeleteTextures(1,&t->tex); free(t); return gpu_fail("texture upload failed"); }
    t->id=id; t->w=w; t->h=h;
    if (old) {
        struct texture **next=&s->textures;
        while (*next!=old) next=&(*next)->next;
        t->next=old->next; *next=t; glDeleteTextures(1,&old->tex); free(old);
    } else { t->next=s->textures; s->textures=t; s->ntexture++; }
    s->texture_bytes=s->texture_bytes-before+after; all_bytes=all_bytes-before+after;
    return 0;
}

static int blend_source(uint32_t n) { return n==0 || n==1 || n==770 || n==771; }
static int blend_destination(uint32_t n) { return blend_source(n) || n==768; }
int scene_frame(struct scene **state, const uint8_t *p, size_t bytes) {
    if (!scene_supported()) return gpu_fail("GPU scenes unavailable");
    if (!p || bytes<8 || bytes%4 || bytes>SCENE_COMMAND_BYTES) return fail("invalid frame size");
    uint32_t w=word(p,0), h=word(p,1);
    if (!w || !h || w>SCENE_SIDE || h>SCENE_SIDE) return fail("invalid framebuffer size");
    size_t n=bytes/4, at=2;
    while (at<n) {
        uint32_t op=word(p,at++);
        size_t count;
        if (op==1) count=4;
        else if (op==2) {
            if (n-at<6 || word(p,at+1)>15 || !blend_source(word(p,at+2)) ||
                !blend_destination(word(p,at+3)) || word(p,at+4)>2) return fail("invalid draw state");
            uint32_t vertices=word(p,at+5);
            if (vertices>65536 || vertices%3) return fail("invalid vertex count");
            count=(size_t)vertices*8; at+=6;
        } else return fail("unknown command");
        if (count>n-at) return fail("truncated command");
        for (size_t i=0;i<count;i++) {
            float f; uint32_t bits=word(p,at+i); memcpy(&f,&bits,4);
            if (!isfinite(f)) return fail("nonfinite value");
        }
        at+=count;
    }
    uint32_t *commands=malloc(bytes-8 ? bytes-8 : 4);
    if (!commands) return gpu_fail("allocation failed");
    memcpy(commands,p+8,bytes-8);
    struct scene *s=get(state);
    if (!s) { free(commands); return gpu_fail("allocation failed"); }
    free(s->commands); s->commands=commands; s->command_bytes=bytes-8; s->w=w; s->h=h; s->dirty=1;
    return 0;
}

struct saved {
    GLint fb, viewport[4], program, array, active, texture, unpack;
    GLint src_rgb, dst_rgb, src_alpha, dst_alpha, eq_rgb, eq_alpha;
    GLboolean blend, scissor, depth, cull, color_mask[4];
    GLfloat clear[4];
    struct { GLint enabled, size, type, normalized, stride, buffer; void *pointer; } attr[3];
};
static void save(struct saved *s) {
    glGetIntegerv(GL_FRAMEBUFFER_BINDING,&s->fb); glGetIntegerv(GL_VIEWPORT,s->viewport);
    glGetIntegerv(GL_CURRENT_PROGRAM,&s->program); glGetIntegerv(GL_ARRAY_BUFFER_BINDING,&s->array);
    glGetIntegerv(GL_ACTIVE_TEXTURE,&s->active); glActiveTexture(GL_TEXTURE0);
    glGetIntegerv(GL_TEXTURE_BINDING_2D,&s->texture); glGetIntegerv(GL_UNPACK_ALIGNMENT,&s->unpack);
    glGetIntegerv(GL_BLEND_SRC_RGB,&s->src_rgb); glGetIntegerv(GL_BLEND_DST_RGB,&s->dst_rgb);
    glGetIntegerv(GL_BLEND_SRC_ALPHA,&s->src_alpha); glGetIntegerv(GL_BLEND_DST_ALPHA,&s->dst_alpha);
    glGetIntegerv(GL_BLEND_EQUATION_RGB,&s->eq_rgb); glGetIntegerv(GL_BLEND_EQUATION_ALPHA,&s->eq_alpha);
    s->blend=glIsEnabled(GL_BLEND); s->scissor=glIsEnabled(GL_SCISSOR_TEST);
    s->depth=glIsEnabled(GL_DEPTH_TEST); s->cull=glIsEnabled(GL_CULL_FACE);
    glGetFloatv(GL_COLOR_CLEAR_VALUE,s->clear);
    glGetBooleanv(GL_COLOR_WRITEMASK,s->color_mask);
    for (unsigned i=0;i<3;i++) {
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_ENABLED,&s->attr[i].enabled);
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_SIZE,&s->attr[i].size);
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_TYPE,&s->attr[i].type);
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_NORMALIZED,&s->attr[i].normalized);
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_STRIDE,&s->attr[i].stride);
        glGetVertexAttribiv(i,GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING,&s->attr[i].buffer);
        glGetVertexAttribPointerv(i,GL_VERTEX_ATTRIB_ARRAY_POINTER,&s->attr[i].pointer);
    }
}
static void enable(GLenum value, int on) { if (on) glEnable(value); else glDisable(value); }
static void restore(const struct saved *s) {
    glBindFramebuffer(GL_FRAMEBUFFER,(GLuint)s->fb); glViewport(s->viewport[0],s->viewport[1],s->viewport[2],s->viewport[3]);
    glUseProgram((GLuint)s->program); glBindTexture(GL_TEXTURE_2D,(GLuint)s->texture); glActiveTexture((GLenum)s->active);
    glPixelStorei(GL_UNPACK_ALIGNMENT,s->unpack);
    glBlendFuncSeparate((GLenum)s->src_rgb,(GLenum)s->dst_rgb,(GLenum)s->src_alpha,(GLenum)s->dst_alpha);
    glBlendEquationSeparate((GLenum)s->eq_rgb,(GLenum)s->eq_alpha);
    enable(GL_BLEND,s->blend); enable(GL_SCISSOR_TEST,s->scissor); enable(GL_DEPTH_TEST,s->depth); enable(GL_CULL_FACE,s->cull);
    glClearColor(s->clear[0],s->clear[1],s->clear[2],s->clear[3]);
    glColorMask(s->color_mask[0],s->color_mask[1],s->color_mask[2],s->color_mask[3]);
    for (unsigned i=0;i<3;i++) {
        glBindBuffer(GL_ARRAY_BUFFER,(GLuint)s->attr[i].buffer);
        glVertexAttribPointer(i,s->attr[i].size,(GLenum)s->attr[i].type,(GLboolean)s->attr[i].normalized,
                              s->attr[i].stride,s->attr[i].pointer);
        if (s->attr[i].enabled) glEnableVertexAttribArray(i); else glDisableVertexAttribArray(i);
    }
    glBindBuffer(GL_ARRAY_BUFFER,(GLuint)s->array);
}

static int target(struct scene *s) {
    if (s->fw==s->w && s->fh==s->h) return 0;
    GLuint tex=0, fbo=0;
    glGenTextures(1,&tex); glBindTexture(GL_TEXTURE_2D,tex);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,s->w,s->h,0,GL_RGBA,GL_UNSIGNED_BYTE,NULL);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST); glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE); glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    glGenFramebuffers(1,&fbo); glBindFramebuffer(GL_FRAMEBUFFER,fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,tex,0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER)!=GL_FRAMEBUFFER_COMPLETE || glGetError()!=GL_NO_ERROR) {
        glDeleteTextures(1,&tex); glDeleteFramebuffers(1,&fbo); return fail("framebuffer allocation failed");
    }
    if (s->tex) glDeleteTextures(1,&s->tex);
    if (s->fbo) glDeleteFramebuffers(1,&s->fbo);
    s->tex=tex; s->fbo=fbo; s->fw=s->w; s->fh=s->h;
    return 1;
}

unsigned scene_render(struct scene *s) {
    if (!program || !s || !s->commands) return 0;
    if (!s->dirty) return s->tex;
    struct saved saved; save(&saved);
    for (int i=0;i<8 && glGetError()!=GL_NO_ERROR;i++) {}
    int resized=target(s);
    if (resized<0) { restore(&saved); return 0; }
    glBindFramebuffer(GL_FRAMEBUFFER,s->fbo); glViewport(0,0,s->w,s->h);
    glDisable(GL_SCISSOR_TEST); glDisable(GL_DEPTH_TEST); glDisable(GL_CULL_FACE); glBlendEquation(GL_FUNC_ADD);
    glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_TRUE);
    if (resized) { glClearColor(0,0,0,0); glClear(GL_COLOR_BUFFER_BIT); }
    glUseProgram(program); glUniform2f(u_size,(float)s->w,(float)s->h); glUniform1i(u_texture,0);
    if (!s->vbo) glGenBuffers(1,&s->vbo);
    glBindBuffer(GL_ARRAY_BUFFER,s->vbo); glBufferData(GL_ARRAY_BUFFER,s->command_bytes,s->commands,GL_STREAM_DRAW);
    for (unsigned i=0;i<3;i++) glEnableVertexAttribArray(i);
    size_t at=0, n=s->command_bytes/4;
    while (at<n) {
        uint32_t op=s->commands[at++];
        if (op==1) {
            glClearColor(number(s->commands+at),number(s->commands+at+1),number(s->commands+at+2),number(s->commands+at+3));
            glClear(GL_COLOR_BUFFER_BIT); at+=4;
        } else {
            const uint32_t *h=s->commands+at;
            struct texture *t=h[0] ? find(s,h[0]) : NULL;
            uint32_t flags=h[1], count=h[5]; at+=6;
            if (!h[0] || t) {
                enable(GL_BLEND,flags&1); glBlendFunc((GLenum)h[2],(GLenum)h[3]);
                glBindTexture(GL_TEXTURE_2D,t ? t->tex : 0);
                glUniform1i(u_sample,t!=NULL); glUniform1i(u_replace,(GLint)h[4]); glUniform1i(u_flags,(GLint)(flags&14));
                glUniform2f(u_tex_size,t ? (float)t->w : 1,t ? (float)t->h : 1);
                glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,32,(void *)(at*4));
                glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,32,(void *)(at*4+8));
                glVertexAttribPointer(2,4,GL_FLOAT,GL_FALSE,32,(void *)(at*4+16));
                glDrawArrays(GL_TRIANGLES,0,(GLsizei)count);
            }
            at+=(size_t)count*8;
        }
    }
    GLenum e=glGetError(); restore(&saved);
    if (e!=GL_NO_ERROR) { fail("GPU draw failed"); return 0; }
    s->dirty=0; return s->tex;
}

void scene_free(struct scene **state) {
    struct scene *s=*state;
    if (!s) return;
    while (s->textures) { struct texture *t=s->textures; s->textures=t->next; glDeleteTextures(1,&t->tex); free(t); }
    all_bytes-=s->texture_bytes;
    if (s->tex) glDeleteTextures(1,&s->tex);
    if (s->fbo) glDeleteFramebuffers(1,&s->fbo);
    if (s->vbo) glDeleteBuffers(1,&s->vbo);
    free(s->commands); free(s); *state=NULL;
}

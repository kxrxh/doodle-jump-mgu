/* Uses the same miniaudio 0.11.25 and decoder settings as miniaudio.py 1.71. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define DR_FLAC_NO_OGG
#define MA_NO_GENERATION
#define MA_NO_ENCODING
#define MA_NO_RESOURCE_MANAGER
#define MA_NO_NODE_GRAPH
#define MA_NO_ENGINE
#define STB_VORBIS_HEADER_ONLY
#include "miniaudio/stb_vorbis.c"
#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio/miniaudio.h"
#undef STB_VORBIS_HEADER_ONLY
#include "miniaudio/stb_vorbis.c"

extern void *audio_create(void);
extern void audio_destroy(void *);
extern const char *audio_error(void *);
extern int audio_load(void *, uint32_t, const int16_t *, size_t, int);
extern int audio_message(void *, int, uint32_t, double, int);
extern int audio_mix(void *, int16_t *, size_t);

typedef struct {
    void *mixer;
    ma_device device;
    int device_ready;
    char error[256];
} HostAudio;

static int failed(HostAudio *audio, const char *message) {
    snprintf(audio->error, sizeof audio->error, "%s", message);
    return -1;
}

static void playback(ma_device *device, void *output, const void *input, ma_uint32 frames) {
    HostAudio *audio = device->pUserData;
    (void)input;
    if (audio_mix(audio->mixer, output, frames))
        memset(output, 0, (size_t)frames * 4);
}

void *host_audio_create(int play) {
    HostAudio *audio = calloc(1, sizeof *audio);
    if (!audio) return NULL;
    audio->mixer = audio_create();
    if (!audio->mixer) { free(audio); return NULL; }
    if (play) {
        ma_device_config config = ma_device_config_init(ma_device_type_playback);
        config.playback.format = ma_format_s16;
        config.playback.channels = 2;
        config.sampleRate = 44100;
        config.periodSizeInMilliseconds = 60;
        config.dataCallback = playback;
        config.pUserData = audio;
        ma_result result = ma_device_init(NULL, &config, &audio->device);
        if (result == MA_SUCCESS) {
            audio->device_ready = 1;
            result = ma_device_start(&audio->device);
            if (result != MA_SUCCESS) {
                ma_device_uninit(&audio->device);
                audio->device_ready = 0;
            }
        }
        if (result != MA_SUCCESS)
            fprintf(stderr, "[audio] No playback device: %s\n", ma_result_description(result));
    }
    return audio;
}

void host_audio_destroy(void *handle) {
    HostAudio *audio = handle;
    if (!audio) return;
    /* Joining miniaudio's callback thread must precede destruction of its mixer. */
    if (audio->device_ready) ma_device_uninit(&audio->device);
    audio_destroy(audio->mixer);
    free(audio);
}

const char *host_audio_error(void *handle) {
    return handle ? ((HostAudio *)handle)->error : "Cannot allocate audio mixer";
}

int host_audio_load(void *handle, uint32_t id, const char *path, int looping) {
    HostAudio *audio = handle;
    if (!audio || !path) return -1;
    ma_decoder_config config = ma_decoder_config_init(ma_format_s16, 2, 44100);
    config.ditherMode = ma_dither_mode_none;
    ma_uint64 frames = 0;
    void *samples = NULL;
    ma_result status = ma_decode_file(path, &config, &frames, &samples);
    if (status != MA_SUCCESS) return failed(audio, ma_result_description(status));
    int result = frames > SIZE_MAX / 2 ? -1 : audio_load(audio->mixer, id, samples, (size_t)frames * 2, looping);
    ma_free(samples, NULL);
    if (result) return failed(audio, audio_error(audio->mixer));
    audio->error[0] = 0;
    return 0;
}

int host_audio_message(void *handle, int message, uint32_t id, double gain, int looping) {
    HostAudio *audio = handle;
    if (!audio) return -1;
    if (audio_message(audio->mixer, message, id, gain, looping))
        return failed(audio, audio_error(audio->mixer));
    return 0;
}

int host_audio_mix(void *handle, int16_t *output, size_t frames) {
    HostAudio *audio = handle;
    if (!audio) return -1;
    if (audio_mix(audio->mixer, output, frames))
        return failed(audio, audio_error(audio->mixer));
    return 0;
}

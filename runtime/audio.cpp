#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
struct Sound {
    std::vector<int16_t> samples;
    bool looping;
};
struct Voice {
    size_t at;
    double volume;
    int loops;
    bool paused;
};
struct Mixer {
    std::mutex mutex;
    std::map<uint32_t, Sound> sounds;
    std::map<uint32_t, Voice> voices;
    std::vector<int64_t> accumulation;
    bool paused = false;
};
thread_local std::string error;

template <class F> int checked(F operation) {
    try {
        operation();
        error.clear();
        return 0;
    } catch (const std::exception &failure) {
        error = failure.what();
    } catch (...) {
        error = "Unknown audio mixer error";
    }
    return -1;
}

double volume(double value) {
    if (!std::isfinite(value))
        throw std::invalid_argument("Invalid sound volume");
    return std::max(0.0, std::min(1.0, value));
}

int64_t rounded(double value) {
    // Preserve Python's ties-to-even PCM scaling without depending on the host rounding mode.
    const double lower = std::floor(value);
    const double fraction = value - lower;
    return static_cast<int64_t>(
        lower + (fraction > .5 || (fraction == .5 && static_cast<int64_t>(lower) % 2)));
}
} // namespace

extern "C" {
void *audio_create() {
    Mixer *result = nullptr;
    checked([&] { result = new Mixer; });
    return result;
}

void audio_destroy(void *handle) { delete static_cast<Mixer *>(handle); }

const char *audio_error(void *) { return error.c_str(); }

int audio_load(void *handle, uint32_t identifier, const int16_t *samples, size_t count,
               int looping) {
    return checked([&] {
        if (!handle || count % 2 || (count && !samples))
            throw std::invalid_argument("Invalid stereo PCM");
        Sound sound{{}, looping != 0};
        if (count)
            sound.samples.assign(samples, samples + count);
        auto &mixer = *static_cast<Mixer *>(handle);
        std::lock_guard<std::mutex> lock(mixer.mutex);
        mixer.sounds[identifier] = std::move(sound);
        mixer.voices.erase(identifier);
    });
}

int audio_message(void *handle, int message, uint32_t identifier, double gain, int loops) {
    return checked([&] {
        if (!handle)
            throw std::invalid_argument("Audio mixer is closed");
        auto &mixer = *static_cast<Mixer *>(handle);
        std::lock_guard<std::mutex> lock(mixer.mutex);
        auto voice = mixer.voices.find(identifier);
        switch (message) {
        case 7:
            mixer.sounds.erase(identifier);
            mixer.voices.erase(identifier);
            break;
        case 8:
            if (mixer.sounds.count(identifier))
                mixer.voices[identifier] = {0, volume(gain), loops, false};
            break;
        case 9:
            mixer.voices.erase(identifier);
            break;
        case 10:
        case 11:
            if (voice != mixer.voices.end())
                voice->second.paused = message == 10;
            break;
        case 12:
        case 13:
            mixer.paused = message == 12;
            break;
        case 14:
            mixer.voices.clear();
            break;
        case 15:
            if (voice != mixer.voices.end())
                voice->second.volume = volume(gain);
            break;
        }
    });
}

int audio_mix(void *handle, int16_t *output, size_t frames) {
    return checked([&] {
        if (!handle || frames > (1u << 20) || (frames && !output))
            throw std::invalid_argument("Invalid audio buffer");
        auto &mixer = *static_cast<Mixer *>(handle);
        std::lock_guard<std::mutex> lock(mixer.mutex);
        const size_t count = frames * 2;
        mixer.accumulation.assign(count, 0);
        if (!mixer.paused) {
            for (auto current = mixer.voices.begin(); current != mixer.voices.end();) {
                auto &voice = current->second;
                if (voice.paused) {
                    ++current;
                    continue;
                }
                const auto &sound = mixer.sounds.at(current->first);
                size_t offset = 0;
                bool finished = sound.samples.empty();
                while (offset < count && !finished) {
                    const size_t chunk = std::min(count - offset, sound.samples.size() - voice.at);
                    for (size_t i = 0; i < chunk; ++i)
                        mixer.accumulation[offset + i] +=
                            rounded(sound.samples[voice.at + i] * voice.volume);
                    offset += chunk;
                    voice.at += chunk;
                    if (voice.at == sound.samples.size()) {
                        if (sound.looping || voice.loops) {
                            voice.at = 0;
                            if (voice.loops > 0)
                                --voice.loops;
                        } else
                            finished = true;
                    }
                }
                if (finished)
                    current = mixer.voices.erase(current);
                else
                    ++current;
            }
        }
        for (size_t i = 0; i < count; ++i)
            output[i] = static_cast<int16_t>(
                std::max<int64_t>(-32768, std::min<int64_t>(32767, mixer.accumulation[i])));
    });
}
}

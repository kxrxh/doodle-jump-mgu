#include "texture.h"

#include <algorithm>
#include <array>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <pthread.h>
#include <thread>

extern "C" void raster_compact(uint8_t *, int, int, const float *, int, const TextureView *,
                               int, int, int, int, int, int, int, int, int);
extern "C" void raster_compact_rows(uint8_t *, int, int, const float *, int, const TextureView *,
                                    int, int, int, int, int, int, int, int, int, int, int);

namespace {
struct Job {
    uint8_t *out;
    int width, height;
    const float *vertices;
    int count;
    const TextureView *texture;
    int tw, th, linear, repeat_s, repeat_t, blend, source, destination, replace;
    int y0, y1;
    unsigned stripes = 1;
    fenv_t environment;

    void render(unsigned stripe) const noexcept {
        const int begin = y0 + int(int64_t(y1 - y0) * stripe / stripes);
        const int end = y0 + int(int64_t(y1 - y0) * (stripe + 1) / stripes);
        raster_compact_rows(out, width, height, vertices, count, texture, tw, th, linear,
                            repeat_s, repeat_t, blend, source, destination, replace, begin, end);
    }
};

class RowPool {
    struct Worker {
        RowPool *pool;
        unsigned stripe;
        pthread_t thread;
    };
    std::array<Worker, 2> workers;
    unsigned worker_count = 0;
    pthread_mutex_t submissions = PTHREAD_MUTEX_INITIALIZER;
    pthread_mutex_t state = PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t work_ready = PTHREAD_COND_INITIALIZER;
    pthread_cond_t finished = PTHREAD_COND_INITIALIZER;
    uint64_t generation = 0;
    unsigned remaining = 0, failed = 0;
    bool stopping = false;
    const Job *current = nullptr;

    static void *entry(void *context) noexcept {
        auto &worker = *static_cast<Worker *>(context);
        worker.pool->run(worker.stripe);
        return nullptr;
    }

    void run(unsigned stripe) noexcept {
        uint64_t seen = 0;
        pthread_mutex_lock(&state);
        for (;;) {
            while (!stopping && generation == seen)
                pthread_cond_wait(&work_ready, &state);
            if (stopping)
                break;
            seen = generation;
            const Job *job = current;
            pthread_mutex_unlock(&state);
            const bool ready = fesetenv(&job->environment) == 0;
            if (ready)
                job->render(stripe);
            pthread_mutex_lock(&state);
            if (!ready)
                failed |= 1u << stripe;
            if (--remaining == 0)
                pthread_cond_signal(&finished);
        }
        pthread_mutex_unlock(&state);
    }

    void stop() noexcept {
        pthread_mutex_lock(&state);
        stopping = true;
        pthread_cond_broadcast(&work_ready);
        pthread_mutex_unlock(&state);
        for (unsigned i = 0; i < worker_count; ++i)
            pthread_join(workers[i].thread, nullptr);
        worker_count = 0;
    }

public:
    RowPool() noexcept {
        const unsigned stripes = std::max(1u, std::min(3u, std::thread::hardware_concurrency()));
        for (unsigned i = 1; i < stripes; ++i) {
            auto &worker = workers[worker_count];
            worker.pool = this;
            worker.stripe = i;
            if (pthread_create(&worker.thread, nullptr, entry, &worker)) {
                stop();
                return;
            }
            ++worker_count;
        }
    }

    ~RowPool() {
        stop();
        pthread_cond_destroy(&finished);
        pthread_cond_destroy(&work_ready);
        pthread_mutex_destroy(&state);
        pthread_mutex_destroy(&submissions);
    }

    void draw(Job &job) noexcept {
        pthread_mutex_lock(&submissions);
        job.stripes = worker_count + 1;
        if (!worker_count) {
            job.render(0);
            pthread_mutex_unlock(&submissions);
            return;
        }
        pthread_mutex_lock(&state);
        current = &job;
        failed = 0;
        remaining = worker_count;
        ++generation;
        pthread_cond_broadcast(&work_ready);
        pthread_mutex_unlock(&state);
        job.render(0);
        pthread_mutex_lock(&state);
        while (remaining)
            pthread_cond_wait(&finished, &state);
        const unsigned redo = failed;
        current = nullptr;
        pthread_mutex_unlock(&state);
        for (unsigned stripe = 1; stripe < job.stripes; ++stripe)
            if (redo & (1u << stripe))
                job.render(stripe);
        pthread_mutex_unlock(&submissions);
    }
};

bool visible_rows(int width, int height, const float *vertices, int count, int &y0, int &y1) noexcept {
    if (width <= 0 || height <= 0 || count < 3 || !vertices)
        return false;
    double left = width, top = height, right = 0, bottom = 0;
    for (int i = 0; i < count - count % 3; ++i) {
        const double x = vertices[i * 8], y = vertices[i * 8 + 1];
        if (!std::isfinite(x) || !std::isfinite(y))
            return false;
        left = std::min(left, x);
        top = std::min(top, y);
        right = std::max(right, x);
        bottom = std::max(bottom, y);
    }
    const int x0 = int(std::floor(std::max(0.0, std::min(double(width), left))));
    const int x1 = int(std::ceil(std::max(0.0, std::min(double(width), right))));
    y0 = int(std::floor(std::max(0.0, std::min(double(height), top))));
    y1 = int(std::ceil(std::max(0.0, std::min(double(height), bottom))));
    return y1 - y0 >= 3 && uint64_t(x1 - x0) * uint64_t(y1 - y0) >= 32768;
}
} // namespace

extern "C" void raster_parallel(uint8_t *out, int width, int height, const float *vertices, int count,
                                 const TextureView *texture, int tw, int th, int linear,
                                 int repeat_s, int repeat_t, int blend, int source,
                                 int destination, int replace) noexcept {
    int y0, y1;
    Job job{out, width, height, vertices, count, texture, tw, th, linear, repeat_s, repeat_t,
            blend, source, destination, replace, 0, height, 1, {}};
    if (!visible_rows(width, height, vertices, count, y0, y1) || fegetenv(&job.environment)) {
        raster_compact(out, width, height, vertices, count, texture, tw, th, linear,
                       repeat_s, repeat_t, blend, source, destination, replace);
        return;
    }
    job.y0 = y0;
    job.y1 = y1;
    static RowPool pool;
    pool.draw(job);
}

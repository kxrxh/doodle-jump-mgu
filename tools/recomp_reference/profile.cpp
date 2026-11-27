// Build-time Unicorn block recording. This is not linked into the AOT runtime.
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <unordered_set>
#include <vector>

struct ProfileBlock { uint64_t address; uint32_t size, cpsr, lr; };
struct ProfileEdge { uint64_t source, target; uint32_t size, from_cpsr, from_lr, to_cpsr; };
namespace {
using ReadRegister = int (*)(void *, int, void *);
struct EdgeHash {
    size_t operator()(const ProfileEdge &e) const {
        uint64_t hash = e.source ^ (uint64_t(e.size) << 32);
        for (uint64_t word : {e.target, uint64_t(e.from_lr),
                              uint64_t((e.from_cpsr & 32) | ((e.to_cpsr & 32) << 1))})
            hash ^= word + 0x9e3779b97f4a7c15ull + (hash << 6) + (hash >> 2);
        return size_t(hash);
    }
};
struct EdgeEqual {
    bool operator()(const ProfileEdge &a, const ProfileEdge &b) const {
        return a.source == b.source && a.target == b.target && a.size == b.size &&
               a.from_lr == b.from_lr && !((a.from_cpsr ^ b.from_cpsr) & 32) &&
               !((a.to_cpsr ^ b.to_cpsr) & 32);
    }
};
struct Profile {
    uint64_t begin, end;
    ReadRegister read;
    size_t max_blocks, max_edges;
    std::vector<ProfileBlock> blocks;
    std::vector<ProfileEdge> edges;
    std::unordered_set<uint64_t> seen_blocks;
    std::unordered_set<ProfileEdge, EdgeHash, EdgeEqual> seen_edges;
    ProfileBlock previous{};
    bool have_previous = false;
    const char *error = "";
    Profile(uint64_t low, uint64_t high, ReadRegister get, size_t block_limit, size_t edge_limit)
        : begin(low), end(high), read(get), max_blocks(block_limit), max_edges(edge_limit) {
        blocks.reserve(std::min(max_blocks, size_t(4096)));
        edges.reserve(std::min(max_edges, size_t(8192)));
    }
};
}
extern "C" void *profile_create(uint64_t begin, uint64_t end, void *read,
                                 size_t max_blocks, size_t max_edges) {
    if (begin >= end || end > (uint64_t(1) << 32) || !read || !max_blocks || !max_edges ||
        max_blocks > 10000000 || max_edges > 10000000) return nullptr;
    try { return new Profile(begin, end, reinterpret_cast<ReadRegister>(read), max_blocks, max_edges); }
    catch (...) { return nullptr; }
}
extern "C" void profile_free(void *profile) { delete static_cast<Profile *>(profile); }
extern "C" const char *profile_error(void *profile) {
    return profile ? static_cast<Profile *>(profile)->error : "Missing block profile";
}
extern "C" const ProfileBlock *profile_blocks(void *profile) {
    return profile ? static_cast<Profile *>(profile)->blocks.data() : nullptr;
}
extern "C" const ProfileEdge *profile_edges(void *profile) {
    return profile ? static_cast<Profile *>(profile)->edges.data() : nullptr;
}
extern "C" size_t profile_block_count(void *profile) {
    return profile ? static_cast<Profile *>(profile)->blocks.size() : 0;
}
extern "C" size_t profile_edge_count(void *profile) {
    return profile ? static_cast<Profile *>(profile)->edges.size() : 0;
}
extern "C" void profile_block(void *uc, uint64_t address, uint32_t size, void *user) noexcept {
    auto &p = *static_cast<Profile *>(user);
    if (*p.error) return;
    if (address < p.begin || address >= p.end) { p.have_previous = false; return; }
    if (!size || size > p.end - address || size > (1u << 20)) {
        p.error = "Invalid recorded block range"; return;
    }
    try {
        ProfileBlock block{address, size, 0, 0};
        if (p.read(uc, 3, &block.cpsr) || p.read(uc, 10, &block.lr)) {
            p.error = "Cannot read recorded CPU mode and link register"; return;
        }
        uint64_t key = ((address | ((block.cpsr >> 5) & 1)) << 32) | size;
        if (!p.seen_blocks.count(key)) {
            if (p.blocks.size() == p.max_blocks) { p.error = "Block profile capacity exceeded"; return; }
            p.blocks.push_back(block);
            p.seen_blocks.insert(key);
        }
        if (p.have_previous) {
            ProfileEdge edge{p.previous.address, address, p.previous.size, p.previous.cpsr,
                             p.previous.lr, block.cpsr};
            if (!p.seen_edges.count(edge)) {
                if (p.edges.size() == p.max_edges) { p.error = "Edge profile capacity exceeded"; return; }
                p.edges.push_back(edge);
                p.seen_edges.insert(edge);
            }
        }
        p.previous = block;
        p.have_previous = true;
    } catch (...) { p.error = "Cannot allocate block profile records"; }
}

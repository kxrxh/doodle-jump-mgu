// CPU state and helpers for iparecomp-generated ARM/Thumb code.
// See upstream.json and LICENSE for provenance and the original MIT notice.
// Checked guest mappings share decoded textures with the host renderer.
#pragma once

#include <math.h>
#include <stdint.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

// VFPv2 on armv6, NEON on armv7. The register files alias: s[2n]/s[2n+1] are
// the halves of d[n], and q[n] is d[2n]/d[2n+1]. One array, many views, so an
// aliased write through any of them is visible to the others -- which is what
// the hardware does and what NEON code relies on.
typedef union {
  uint64_t u64[32];   // d0-d31
  int64_t i64[32];
  double f64[32];
  uint32_t u32[64];   // s0-s31 occupy the first 32 of these
  int32_t i32[64];
  float f32[64];
  uint16_t u16[128];
  uint8_t u8[256];
} Arm32VecFile;

typedef struct Arm32Ctx {
  // PC tracks dispatch and returns; instruction PC reads are folded at build time.
  uint32_t r[16];

  // NZCV, kept unpacked. Nothing reads them as a word, and keeping them apart
  // means a flag-setting instruction writes four independent variables the
  // host compiler can forward, instead of a packed CPSR nobody can see into.
  uint32_t nf, zf, cf, vf;
  uint32_t qf;  // saturation sticky bit, set by QADD/SSAT and friends

  Arm32VecFile v;
  uint32_t fpscr;

  // Where the image was mapped. Every address the instruction stream derived
  // from the PC -- literal pool loads, adr, the constants behind
  // position-independent stubs -- is emitted as image_base + a constant.
  uint32_t image_base;

  // Predicates are resolved at build time; saved contexts retain the guest ABI.
  uint32_t itstate;
  uint32_t cpsr, cpacr, fpexc;

  // Architectural context saves stop before these engine-owned pointers.
  void* owner;
  uint8_t** pages;
  uint8_t* permissions;
} Arm32Ctx;

// --- register access -------------------------------------------------------
// No zero register on this architecture, so these are plain accesses. They
// exist so the emitter has one spelling to change, and so that a read of r15
// can be trapped in a debug build.
#define ARC_R(c, n) ((c)->r[(n)])
#define ARC_W(c, n, val) ((void)((c)->r[(n)] = (uint32_t)(val)))
#define ARC_SP(c) ((c)->r[13])
#define ARC_LR(c) ((c)->r[14])

// --- condition codes -------------------------------------------------------
// The cond field of every ARM instruction, and of Thumb's `it`. Emitted as a
// guard expression around the instruction body, so a predicated instruction
// costs a branch the host compiler can usually flatten into a cmov.
#define ARC_COND_EQ(c) ((c)->zf)
#define ARC_COND_NE(c) (!(c)->zf)
#define ARC_COND_CS(c) ((c)->cf)
#define ARC_COND_CC(c) (!(c)->cf)
#define ARC_COND_MI(c) ((c)->nf)
#define ARC_COND_PL(c) (!(c)->nf)
#define ARC_COND_VS(c) ((c)->vf)
#define ARC_COND_VC(c) (!(c)->vf)
#define ARC_COND_HI(c) ((c)->cf && !(c)->zf)
#define ARC_COND_LS(c) (!(c)->cf || (c)->zf)
#define ARC_COND_GE(c) ((c)->nf == (c)->vf)
#define ARC_COND_LT(c) ((c)->nf != (c)->vf)
#define ARC_COND_GT(c) (!(c)->zf && (c)->nf == (c)->vf)
#define ARC_COND_LE(c) ((c)->zf || (c)->nf != (c)->vf)
#define ARC_COND_AL(c) (1)

// --- flags -----------------------------------------------------------------
#define ARC_NZ(c, res)                     \
  do {                                     \
    (c)->nf = ((uint32_t)(res)) >> 31;     \
    (c)->zf = ((uint32_t)(res)) == 0;      \
  } while (0)

static inline void arc_add_flags(Arm32Ctx* c, uint32_t a, uint32_t b, uint32_t carry_in) {
  uint64_t wide = (uint64_t)a + (uint64_t)b + carry_in;
  uint32_t res = (uint32_t)wide;
  ARC_NZ(c, res);
  c->cf = (uint32_t)(wide >> 32);
  c->vf = (~(a ^ b) & (a ^ res)) >> 31;
}

static inline void arc_sub_flags(Arm32Ctx* c, uint32_t a, uint32_t b, uint32_t carry_in) {
  // SUB is ADD of the complement; the carry-in is the NOT-borrow, which is why
  // `cmp` sets C on *no* borrow. Getting this backwards is the classic 32-bit
  // ARM lifting bug and it only shows up in unsigned comparisons.
  uint64_t wide = (uint64_t)a + (uint64_t)(~b) + carry_in;
  uint32_t res = (uint32_t)wide;
  ARC_NZ(c, res);
  c->cf = (uint32_t)(wide >> 32);
  c->vf = ((a ^ b) & (a ^ res)) >> 31;
}

// --- barrel shifter --------------------------------------------------------
// Operand2 of most data-processing instructions is a register run through a
// shifter, and the shifter emits its own carry-out. That carry is what a
// flag-setting logical instruction writes to C -- the ALU never produced one.
// These return the shifted value and write the carry through a pointer, so a
// non-flag-setting form can pass NULL and the host compiler drops the store.
typedef struct { uint32_t value, carry; } ArcShift;

static inline ArcShift arc_lsl(uint32_t v, uint32_t n, uint32_t cin) {
  ArcShift r;
  if (n == 0) { r.value = v; r.carry = cin; }
  else if (n < 32) { r.value = v << n; r.carry = (v >> (32 - n)) & 1; }
  else if (n == 32) { r.value = 0; r.carry = v & 1; }
  else { r.value = 0; r.carry = 0; }
  return r;
}

static inline ArcShift arc_lsr(uint32_t v, uint32_t n, uint32_t cin) {
  ArcShift r;
  if (n == 0) { r.value = v; r.carry = cin; }
  else if (n < 32) { r.value = v >> n; r.carry = (v >> (n - 1)) & 1; }
  else if (n == 32) { r.value = 0; r.carry = v >> 31; }
  else { r.value = 0; r.carry = 0; }
  return r;
}

static inline ArcShift arc_asr(uint32_t v, uint32_t n, uint32_t cin) {
  ArcShift r;
  if (n == 0) { r.value = v; r.carry = cin; }
  else if (n < 32) {
    r.value = (uint32_t)((int32_t)v >> n);
    r.carry = (v >> (n - 1)) & 1;
  } else {
    r.value = (uint32_t)((int32_t)v >> 31);
    r.carry = v >> 31;
  }
  return r;
}

static inline ArcShift arc_ror(uint32_t v, uint32_t n, uint32_t cin) {
  // The zero test happens *before* the mask, and that distinction is the whole
  // subtlety: ROR by 32, 64 or 96 leaves the value alone but still writes C
  // from bit 31, while ROR by 0 must leave C untouched. Masking first collapses
  // the two and quietly carries a stale flag into the next conditional.
  ArcShift r;
  if (n == 0) { r.value = v; r.carry = cin; return r; }
  n &= 31;
  if (n == 0) { r.value = v; r.carry = v >> 31; }
  else { r.value = (v >> n) | (v << (32 - n)); r.carry = (r.value >> 31) & 1; }
  return r;
}

// RRX: a 33-bit rotate through the carry flag. One bit, no shift amount.
static inline ArcShift arc_rrx(uint32_t v, uint32_t cin) {
  ArcShift r;
  r.value = (v >> 1) | (cin << 31);
  r.carry = v & 1;
  return r;
}

// --- VFP -------------------------------------------------------------------
// The register file aliases, so these are views rather than conversions:
// s[2n] and s[2n+1] are the halves of d[n], which is what the hardware does
// and what the calling convention relies on when a double is passed in two
// single registers.
#define ARC_S(c, n) ((c)->v.f32[(n)])
#define ARC_D(c, n) ((c)->v.f64[(n)])
#define ARC_SU(c, n) ((c)->v.u32[(n)])
#define ARC_DU(c, n) ((c)->v.u64[(n)])

// Three places C and the hardware disagree, each producing a plausible wrong
// number rather than a crash. All three were found the expensive way.
//
// 1. An invalid operation -- 0/0, inf - inf, 0 * inf -- yields a NaN whose
//    sign bit x86 sets and ARM clears. It affects all four basic operations,
//    not just sqrt. ARM returns its *default* NaN, which is positive.
// 2. Float-to-integer saturates on ARM and is undefined behaviour in C once
//    the value does not fit, so it cannot be written as a cast at all.
// 3. (No vmin/vmax in the armv6 encodings these binaries use. If a later
//    target brings them: ARM propagates NaN, C's fmin/fmax deliberately do
//    not, and only fminnm/fmaxnm match C.)
#define ARC_DEFAULT_NAN_F32 0x7FC00000u
#define ARC_DEFAULT_NAN_F64 0x7FF8000000000000ull

static inline float arc_dnan_f32(void) {
  union { uint32_t u; float f; } x;
  x.u = ARC_DEFAULT_NAN_F32;
  return x.f;
}

static inline double arc_dnan_f64(void) {
  union { uint64_t u; double f; } x;
  x.u = ARC_DEFAULT_NAN_F64;
  return x.f;
}

// A NaN the operation *generated* rather than propagated is the default NaN.
// A NaN that came in from an operand is passed through as the hardware would.
#define ARC_FP_OP(T, SUFFIX, EXPR)                                        \
  static inline T arc_##SUFFIX(T a, T b) {                                \
    T r = (EXPR);                                                         \
    if ((r != r) && (a == a) && (b == b)) return arc_dnan_##T##_();       \
    return r;                                                             \
  }

static inline float arc_dnan_float_(void) { return arc_dnan_f32(); }
static inline double arc_dnan_double_(void) { return arc_dnan_f64(); }

ARC_FP_OP(float, addf32, a + b)
ARC_FP_OP(float, subf32, a - b)
ARC_FP_OP(float, mulf32, a * b)
ARC_FP_OP(float, divf32, a / b)
ARC_FP_OP(double, addf64, a + b)
ARC_FP_OP(double, subf64, a - b)
ARC_FP_OP(double, mulf64, a * b)
ARC_FP_OP(double, divf64, a / b)

static inline float arc_sqrtf32(float a) {
  if (a < 0.0f) return arc_dnan_f32();
  return (float)sqrt((double)a);
}

static inline double arc_sqrtf64(double a) {
  if (a < 0.0) return arc_dnan_f64();
  return sqrt(a);
}

// Saturating, and never a bare cast: the out-of-range case is undefined
// behaviour in C, which in practice means the optimiser is entitled to assume
// it cannot happen and delete the guard you wrote after the cast.
static inline uint32_t arc_f32_to_s32(float v) {
  if (v != v) return 0;
  if (v >= 2147483648.0f) return 0x7FFFFFFFu;
  if (v < -2147483648.0f) return 0x80000000u;
  return (uint32_t)(int32_t)v;
}

static inline uint32_t arc_f32_to_u32(float v) {
  if (v != v || v <= 0.0f) return 0;
  if (v >= 4294967296.0f) return 0xFFFFFFFFu;
  return (uint32_t)v;
}

static inline uint32_t arc_f64_to_s32(double v) {
  if (v != v) return 0;
  if (v >= 2147483648.0) return 0x7FFFFFFFu;
  if (v < -2147483648.0) return 0x80000000u;
  return (uint32_t)(int32_t)v;
}

static inline uint32_t arc_f64_to_u32(double v) {
  if (v != v || v <= 0.0) return 0;
  if (v >= 4294967296.0) return 0xFFFFFFFFu;
  return (uint32_t)v;
}

// A VFP compare writes FPSCR, not the core flags, and a separate `vmrs
// apsr_nzcv, fpscr` moves them across. Keeping that two-step is not pedantry:
// there are usually several instructions between the compare and the transfer,
// and collapsing them would let an intervening integer instruction that sets
// flags be silently overwritten.
#define ARC_FPSCR_N 0x80000000u
#define ARC_FPSCR_Z 0x40000000u
#define ARC_FPSCR_C 0x20000000u
#define ARC_FPSCR_V 0x10000000u

static inline uint32_t arc_fp_compare(int lt, int eq, int unordered) {
  // Unordered sets C and V and clears N and Z, which is what makes an
  // unsigned-style condition (`hi`, `ls`) the way a NaN-safe float comparison
  // is spelled on this architecture.
  if (unordered) return ARC_FPSCR_C | ARC_FPSCR_V;
  if (eq) return ARC_FPSCR_Z | ARC_FPSCR_C;
  if (lt) return ARC_FPSCR_N;
  return ARC_FPSCR_C;
}

static inline void arc_vcmp_f32(Arm32Ctx* c, float a, float b) {
  c->fpscr = (c->fpscr & 0x0FFFFFFFu) |
             arc_fp_compare(a < b, a == b, (a != a) || (b != b));
}

static inline void arc_vcmp_f64(Arm32Ctx* c, double a, double b) {
  c->fpscr = (c->fpscr & 0x0FFFFFFFu) |
             arc_fp_compare(a < b, a == b, (a != a) || (b != b));
}

static inline void arc_vmrs_nzcv(Arm32Ctx* c) {
  c->nf = (c->fpscr & ARC_FPSCR_N) != 0;
  c->zf = (c->fpscr & ARC_FPSCR_Z) != 0;
  c->cf = (c->fpscr & ARC_FPSCR_C) != 0;
  c->vf = (c->fpscr & ARC_FPSCR_V) != 0;
}

// --- memory ----------------------------------------------------------------
uint64_t arc_load(Arm32Ctx* c, uint32_t address, size_t size);
void arc_store(Arm32Ctx* c, uint32_t address, uint64_t value, size_t size);

// Fixed widths stay as loads/stores even when a large function outlines these helpers.
#define ARC_MEMORY(bits, type) \
static inline uint64_t arc_read##bits(Arm32Ctx* c, uint32_t a) { \
  unsigned page = a >> 12, offset = a & 4095u; \
  if (sizeof(type) <= 4096u - offset && c->pages && c->pages[page] && (c->permissions[page] & 1u)) { \
    type value; \
    memcpy(&value, c->pages[page] + offset, sizeof(value)); \
    return value; \
  } \
  return arc_load(c, a, sizeof(type)); \
} \
static inline void arc_write##bits(Arm32Ctx* c, uint32_t a, type value) { \
  unsigned page = a >> 12, offset = a & 4095u; \
  if (sizeof(type) <= 4096u - offset && c->pages && c->pages[page] && (c->permissions[page] & 2u)) { \
    memcpy(c->pages[page] + offset, &value, sizeof(value)); \
    return; \
  } \
  arc_store(c, a, value, sizeof(type)); \
}
ARC_MEMORY(8, uint8_t)
ARC_MEMORY(16, uint16_t)
ARC_MEMORY(32, uint32_t)
ARC_MEMORY(64, uint64_t)
#undef ARC_MEMORY
#define ARC_LD8(a) ((uint32_t)arc_read8(c, (uint32_t)(a)))
#define ARC_LD8S(a) ((uint32_t)(int32_t)(int8_t)ARC_LD8(a))
#define ARC_LD16(a) ((uint32_t)arc_read16(c, (uint32_t)(a)))
#define ARC_LD16S(a) ((uint32_t)(int32_t)(int16_t)ARC_LD16(a))
#define ARC_LD32(a) ((uint32_t)arc_read32(c, (uint32_t)(a)))
#define ARC_LD64(a) arc_read64(c, (uint32_t)(a))
#define ARC_ST8(a, v) arc_write8(c, (uint32_t)(a), (uint8_t)(v))
#define ARC_ST16(a, v) arc_write16(c, (uint32_t)(a), (uint16_t)(v))
#define ARC_ST32(a, v) arc_write32(c, (uint32_t)(a), (uint32_t)(v))
#define ARC_ST64(a, v) arc_write64(c, (uint32_t)(a), (uint64_t)(v))

// --- control flow ----------------------------------------------------------
// A guest trap: an undefined instruction, or an indirect branch that resolves
// to nothing. Loud on purpose -- reaching one means the lift is incomplete.
void arc_trap(Arm32Ctx* c, const char* what);
int arc_enter(Arm32Ctx* c, uint32_t address, uint32_t bytes);
void arc_block(Arm32Ctx* c, uint32_t address, uint32_t bytes);
void arc_return(Arm32Ctx* c, uint32_t target);
typedef void (*ArcDispatchFn)(Arm32Ctx*, uint32_t);
void arc_dispatch(Arm32Ctx* c, uint32_t target);
void arc_dispatch_miss(Arm32Ctx* c, uint32_t target);

#ifdef __cplusplus
}
#endif

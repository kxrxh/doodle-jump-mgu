#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
static uint64_t calls,bytes,nanos;
int graphics_texture_rgba(void *g,uint32_t id,void*out,size_t n){
 static int(*next)(void*,uint32_t,void*,size_t);
 if(!next)next=dlsym(RTLD_NEXT,"graphics_texture_rgba");
 struct timespec a,b;clock_gettime(CLOCK_THREAD_CPUTIME_ID,&a);
 int result=next(g,id,out,n);
 clock_gettime(CLOCK_THREAD_CPUTIME_ID,&b);
 calls++;bytes+=n;nanos+=(b.tv_sec-a.tv_sec)*1000000000ull+b.tv_nsec-a.tv_nsec;
 return result;
}
__attribute__((destructor))static void report(void){fprintf(stderr,"PAPER_PROBE calls=%llu bytes=%llu cpu_ns=%llu\n",(unsigned long long)calls,(unsigned long long)bytes,(unsigned long long)nanos);}

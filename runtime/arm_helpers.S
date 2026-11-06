.syntax unified
.arm
.text
// Small XML/font allocations stay inside ARM; large allocations use the host heap.
.type malloc_native,%function
malloc_native:
    cmp r0,#4096
    bhi 5f
    push {r0}
    mov r1,#16
    mov r2,#4
1:  cmp r0,r1
    bls 2f
    lsl r1,r1,#1
    add r2,r2,#1
    b 1b
2:  ldr r3,=0x01101000
    add r12,r3,#4
    ldr r0,[r12,r2,lsl #2]
    cmp r0,#0
    beq 3f
    ldr r1,[r0]
    str r1,[r12,r2,lsl #2]
    b 4f
3:  ldr r0,[r3]
    add r1,r1,#16
    add r12,r0,r1
    ldr r1,=0x41000000
    cmp r12,r1
    bhi 6f
    str r12,[r3]
    add r0,r0,#16
4:  str r2,[r0,#-16]
    mov r1,#1
    str r1,[r0,#-12]
    add sp,sp,#4
    bx lr
6:  pop {r0}
5:  ldr r12,=0x010fe000
    bx r12

.type free_native,%function
free_native:
    cmp r0,#0
    beq 3f
    ldr r1,=0x40000010
    cmp r0,r1
    blo 4f
    ldr r1,=0x41000000
    cmp r0,r1
    bhs 4f
    tst r0,#15
    bne 4f
    ldr r1,[r0,#-16]
    cmp r1,#4
    blo 4f
    cmp r1,#12
    bhi 4f
    ldr r12,[r0,#-12]
    cmp r12,#1
    bne 4f
    ldr r2,=0x01101004
    ldr r3,[r2,r1,lsl #2]
    str r3,[r0]
    str r0,[r2,r1,lsl #2]
    mov r12,#0
    str r12,[r0,#-12]
3:  mov r0,#0
    bx lr
4:  ldr r12,=0x010fe004
    bx r12

.type memcpy_native,%function
.type __aeabi_memcpy_native,%function
memcpy_native:
__aeabi_memcpy_native:
    push {r0}
    orr r3,r0,r1
    tst r3,#3
    bne 2f
1:  cmp r2,#4
    blo 2f
    ldr r3,[r1],#4
    str r3,[r0],#4
    sub r2,r2,#4
    b 1b
2:  cmp r2,#0
    beq 4f
3:  ldrb r3,[r1],#1
    strb r3,[r0],#1
    subs r2,r2,#1
    bne 3b
4:  pop {r0}
    bx lr

.type memmove_native,%function
.type __aeabi_memmove_native,%function
memmove_native:
__aeabi_memmove_native:
    cmp r0,r1
    bls memcpy_native
    push {r0}
    add r0,r0,r2
    add r1,r1,r2
    cmp r2,#0
    beq 2f
1:  ldrb r3,[r1,#-1]!
    strb r3,[r0,#-1]!
    subs r2,r2,#1
    bne 1b
2:  pop {r0}
    bx lr

.type memset_native,%function
memset_native:
    push {r0}
    cmp r2,#0
    beq 2f
1:  strb r1,[r0],#1
    subs r2,r2,#1
    bne 1b
2:  pop {r0}
    bx lr

.type __aeabi_memset_native,%function
__aeabi_memset_native:
    mov r3,r1
    mov r1,r2
    mov r2,r3
    b memset_native

.type strlen_native,%function
strlen_native:
    mov r1,r0
1:  ldrb r2,[r1],#1
    cmp r2,#0
    bne 1b
    sub r0,r1,r0
    sub r0,r0,#1
    bx lr

.type strcmp_native,%function
strcmp_native:
1:  ldrb r2,[r0],#1
    ldrb r3,[r1],#1
    cmp r2,r3
    bne 2f
    cmp r2,#0
    bne 1b
2:  sub r0,r2,r3
    bx lr

.type strncmp_native,%function
strncmp_native:
    cmp r2,#0
    beq 3f
1:  ldrb r3,[r0],#1
    ldrb r12,[r1],#1
    cmp r3,r12
    bne 2f
    cmp r3,#0
    beq 2f
    subs r2,r2,#1
    bne 1b
3:  mov r0,#0
    bx lr
2:  sub r0,r3,r12
    bx lr

.type pthread_mutex_lock_native,%function
.type pthread_mutex_unlock_native,%function
.type pthread_mutex_init_native,%function
.type pthread_mutex_destroy_native,%function
pthread_mutex_lock_native:
pthread_mutex_unlock_native:
pthread_mutex_init_native:
pthread_mutex_destroy_native:
    mov r0,#0
    bx lr

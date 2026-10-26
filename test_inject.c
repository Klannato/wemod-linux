/* Test target for ctinject: a hookable function + an executable code cave.

Compile:  gcc -no-pie -O0 -o test_inject test_inject.c
Run:      ./test_inject   (prints PID, tick, cave, g_value addresses)
*/
#include <stdio.h>
#include <unistd.h>

volatile int g_value = 1234567;

/* executable buffer in .text (r-x) to hold injected code / mmap stub */
__attribute__((section(".text"), used, aligned(16)))
unsigned char cave[128] = {0x90};

/* hooked function: first 5 bytes = B8 44 33 22 11 (unique AOB pattern) */
__attribute__((noinline, naked))
void tick(void) {
    __asm__ volatile("movl $0x11223344, %eax\n\tret\n\t");
}

/* RIP-relative load: mov eax, [rip+g_value]  (8B 05 <disp32>) then ret — used
   to exercise the readmem RIP-relative fixup. */
__attribute__((noinline, naked))
int hooked(void) {
    __asm__ volatile("movl g_value(%rip), %eax\n\tret\n\t");
}

int main(void) {
    printf("PID=%d tick=%p hooked=%p cave=%p g_value=%p\n",
           getpid(), (void*)tick, (void*)hooked, (void*)cave, (void*)&g_value);
    fflush(stdout);
    for (int i = 0; i < 20; i++) {
        tick();
        printf("g_value=%d hooked=%d\n", g_value, hooked());
        fflush(stdout);
        sleep(1);
    }
    return 0;
}

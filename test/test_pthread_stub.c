#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
extern int pthread_create(pthread_t * __newthread, const pthread_attr_t  *__attr, void *(*__start_routine) (void *), void * __arg){
    return EAGAIN;
}


static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
int main (void) {
    int return_value = 0;
    uint64_t t0 = now_ns();
    uint64_t t1 = now_ns();
    while( (t1 - t0) < 3000000000ULL){
        return_value++;
        t1 = now_ns();
    }
    printf("STUB DONE");
    return 0;
}
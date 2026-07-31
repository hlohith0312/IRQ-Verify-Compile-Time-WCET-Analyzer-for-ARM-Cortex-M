/* fake stdlib.h — declarations only, no implementation */
#ifndef _STDLIB_H
#define _STDLIB_H
#include <stddef.h>
void *malloc(size_t size);
void  free(void *ptr);
void *realloc(void *ptr, size_t size);
void *calloc(size_t nmemb, size_t size);
void  abort(void);
void  exit(int status);
#endif

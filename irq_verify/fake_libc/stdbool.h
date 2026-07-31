/* fake stdbool.h — minimal for pycparser compatibility */
#ifndef _STDBOOL_H
#define _STDBOOL_H
/* _Bool is a C99 keyword; pycparser already knows it.
 * Define bool, true, false only. */
#define bool  int
#define true  1
#define false 0
#endif

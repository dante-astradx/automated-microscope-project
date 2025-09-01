/*
 * C library for image processing.
 *
 * Hazen 06/25
 */

/* Include */
#include <stdint.h>

void update_x_xx_32(uint32_t *x, uint32_t *xx, uint16_t *im, int s0, int s1)
{
  int i0,i1;
  int o0;
  uint32_t t0;

  for(i0=0;i0<s0;i0++){
    o0 = i0*s1;
    for(i1=0;i1<s1;i1++){
      t0 = (uint32_t)im[o0+i1];
      x[o0+i1] += t0;
      xx[o0+i1] += t0*t0;
    }
  }
}

void update_x_xx_64(uint64_t *x, uint64_t *xx, uint16_t *im, int s0, int s1)
{
  int i0,i1;
  int o0;
  uint64_t t0;

  for(i0=0;i0<s0;i0++){
    o0 = i0*s1;
    for(i1=0;i1<s1;i1++){
      t0 = (uint64_t)im[o0+i1];
      x[o0+i1] += t0;
      xx[o0+i1] += t0*t0;
    }
  }
}

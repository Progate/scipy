/*
 * C entry points for the CHARACTER-valued LAPACK functions, for builds without
 * a Fortran compiler (see _character_functions.f90). The LAPACK libraries used
 * there follow the f2c/gfortran convention: the result buffer and its length
 * are passed as the first two arguments.
 */
#include <stddef.h>
#include "scipy_blas_defines.h"

void BLAS_FUNC(chla_transtype)(char *result, size_t result_len, CBLAS_INT *trans);

char scipy_chla_transtype(CBLAS_INT *trans)
{
    char result;
    BLAS_FUNC(chla_transtype)(&result, 1, trans);
    return result;
}

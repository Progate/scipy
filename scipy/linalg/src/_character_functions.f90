! C entry points for the CHARACTER-valued LAPACK functions.
!
! How a CHARACTER function result is returned is compiler specific: gfortran
! and f2c pass a result buffer and its length as the first two arguments, and
! flang on wasm32 additionally returns a descriptor through a struct-return
! pointer. C therefore cannot declare these functions portably. BIND(C) lets
! the Fortran compiler that built LAPACK make the call with its own convention.
function scipy_chla_transtype(trans) bind(c, name='scipy_chla_transtype') result(res)
  use iso_c_binding, only: c_char, c_int
  implicit none
  integer(c_int), intent(in) :: trans
  character(kind=c_char) :: res
  character :: chla_transtype
  external :: chla_transtype
  res = chla_transtype(trans)
end function scipy_chla_transtype

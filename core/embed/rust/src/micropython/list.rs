use core::{convert::TryFrom, ptr};

use crate::error::Error;

use super::{ffi, gc::Gc, obj::Obj, runtime::catch_exception};

pub type List = ffi::mp_obj_list_t;

impl List {
    pub fn alloc(values: &[Obj]) -> Result<Gc<Self>, Error> {
        // SAFETY: Although `values` are copied into the new list and not mutated,
        // `mp_obj_new_list` is taking them through a mut pointer.
        // EXCEPTION: Will raise if allocation fails.
        catch_exception(|| unsafe {
            let list = ffi::mp_obj_new_list(values.len(), values.as_ptr() as *mut Obj);
            Gc::from_raw(list.as_ptr().cast())
        })
    }

    pub fn with_capacity(capacity: usize) -> Result<Gc<Self>, Error> {
        // EXCEPTION: Will raise if allocation fails.
        catch_exception(|| unsafe {
            let list = ffi::mp_obj_new_list(capacity, ptr::null_mut());
            // By default, the new list will have its len set to n. We want to preallocate
            // to a specific size and then use append() to add items, so we reset len to 0.
            ffi::mp_obj_list_set_len(list, 0);
            Gc::from_raw(list.as_ptr().cast())
        })
    }

    pub fn from_iter<T, E>(iter: impl Iterator<Item = T>) -> Result<Gc<List>, Error>
    where
        T: TryInto<Obj, Error = E>,
        Error: From<E>,
    {
        let max_size = iter.size_hint().1.unwrap_or(0);
        let mut gc_list = List::with_capacity(max_size)?;
        let list = unsafe { Gc::as_mut(&mut gc_list) };
        for value in iter {
            list.append(value.try_into()?)?;
        }
        Ok(gc_list)
    }

    // Internal helper to get the `Obj` variant of this.
    // SAFETY: For convenience, the function works on an immutable reference, but
    // the returned `Obj` is inherently mutable.
    // Caller is responsible for ensuring that self is borrowed mutably if any
    // mutation is to occur.
    unsafe fn as_mut_obj(&self) -> Obj {
        unsafe {
            let ptr = self as *const Self as *mut _;
            Obj::from_ptr(ptr)
        }
    }

    pub fn append(&mut self, value: Obj) -> Result<(), Error> {
        unsafe {
            // SAFETY: self is borrowed mutably.
            let list = self.as_mut_obj();
            // EXCEPTION: Will raise if allocation fails.
            catch_exception(|| {
                ffi::mp_obj_list_append(list, value);
            })
        }
    }

    pub fn len(&self) -> usize {
        self.as_slice().len()
    }

    pub fn as_slice(&self) -> &[Obj] {
        unsafe {
            // SAFETY: mp_obj_list_get() does not mutate the list.
            let list = self.as_mut_obj();
            let mut len: usize = 0;
            let mut items_ptr: *mut Obj = ptr::null_mut();
            ffi::mp_obj_list_get(list, &mut len, &mut items_ptr);
            assert!(!items_ptr.is_null());
            core::slice::from_raw_parts(items_ptr, len)
        }
    }

    pub fn as_mut_slice(&mut self) -> &mut [Obj] {
        unsafe {
            // SAFETY: self is borrowed mutably.
            let list = self.as_mut_obj();
            let mut len: usize = 0;
            let mut items_ptr: *mut Obj = ptr::null_mut();
            ffi::mp_obj_list_get(list, &mut len, &mut items_ptr);
            assert!(!items_ptr.is_null());
            core::slice::from_raw_parts_mut(items_ptr, len)
        }
    }
}

impl From<Gc<List>> for Obj {
    fn from(value: Gc<List>) -> Self {
        // SAFETY:
        //  - `value` is an object struct with a base and a type.
        //  - `value` is GC-allocated.
        unsafe { Obj::from_ptr(Gc::into_raw(value).cast()) }
    }
}

impl TryFrom<Obj> for Gc<List> {
    type Error = Error;

    fn try_from(value: Obj) -> Result<Self, Self::Error> {
        if unsafe { ffi::mp_type_list.is_type_of(value) } {
            // SAFETY: We assume that if `value` is an object pointer with the correct type,
            // it is managed by MicroPython GC (see `Gc::from_raw` for details).
            let this = unsafe { Gc::from_raw(value.as_ptr().cast()) };
            Ok(this)
        } else {
            Err(Error::TypeError)
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::micropython::{
        iter::{Iter, IterBuf},
        testutil::mpy_init,
    };

    use super::*;
    use heapless::Vec;

    #[test]
    fn list_from_iter() {
        unsafe { mpy_init() };

        // create an upy list of 5 elements
        let vec: Vec<u8, 10> = (0..5).collect();
        let list: Obj = List::from_iter(vec.iter().copied()).unwrap().into();

        let mut buf = IterBuf::new();
        let iter = Iter::try_from_obj_with_buf(list, &mut buf).unwrap();
        // collect the elements into a Vec of maximum length 10, through an iterator
        let retrieved_vec: Vec<u8, 10> = iter
            .map(TryInto::try_into)
            .collect::<Result<Vec<u8, 10>, Error>>()
            .unwrap();
        assert_eq!(vec, retrieved_vec);
    }

    #[test]
    fn list_len() {
        unsafe { mpy_init() };

        let vec: Vec<u16, 17> = (0..17).collect();
        let list = List::from_iter(vec.iter().copied()).unwrap();
        assert_eq!(list.len(), vec.len());
    }

    #[test]
    fn list_as_slice() {
        unsafe { mpy_init() };

        let vec: Vec<u16, 17> = (13..13 + 17).collect();
        let list = List::from_iter(vec.iter().copied()).unwrap();

        let slice = list.as_slice();
        assert_eq!(slice.len(), vec.len());
        for i in 0..slice.len() {
            assert_eq!(vec[i], slice[i].try_into().unwrap());
        }
    }

    #[test]
    fn list_as_mut_slice() {
        unsafe { mpy_init() };

        let vec: Vec<u16, 5> = (0..5).collect();
        let mut list = List::from_iter(vec.iter().copied()).unwrap();

        let slice = unsafe { Gc::<List>::as_mut(&mut list) }.as_mut_slice();
        assert_eq!(slice.len(), vec.len());
        assert_eq!(vec[0], slice[0].try_into().unwrap());

        for i in 0..slice.len() {
            slice[i] = ((i + 10) as u16).into();
        }

        let mut buf = IterBuf::new();
        let iter = Iter::try_from_obj_with_buf(list.into(), &mut buf).unwrap();
        let retrieved_vec: Vec<u16, 5> = iter
            .map(TryInto::try_into)
            .collect::<Result<Vec<u16, 5>, Error>>()
            .unwrap();

        for i in 0..retrieved_vec.len() {
            assert_eq!(retrieved_vec[i], vec[i] + 10);
        }
    }
}

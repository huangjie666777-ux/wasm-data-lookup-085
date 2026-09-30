;; Prints endlessly: must be terminated by the combined-output byte cap.
(module
  (import "wasi_snapshot_preview1" "fd_write"
    (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))
  (memory (export "memory") 1)
  (data (i32.const 8) "XXXXXXXXXX")
  (func (export "_start")
    i32.const 0 i32.const 8 i32.store
    i32.const 4 i32.const 10 i32.store
    loop $spam
      i32.const 1 i32.const 0 i32.const 1 i32.const 100
      call $fd_write drop
      br $spam
    end
    i32.const 0 call $proc_exit))

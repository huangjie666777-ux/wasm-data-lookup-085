;; Data-transformation WASI preview1 program.
;; Reads stdin until EOF, ASCII-uppercases it, writes the result to stdout.
;;
;; Memory layout:
;;   0..7    read iovec  { buf=64, len=4096 }
;;   8..15   write iovec { buf=64, len=n }
;;   16..19  read nwritten
;;   24..27  write nwritten
;;   64..    data buffer
(module
  (import "wasi_snapshot_preview1" "fd_read"
    (func $fd_read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write"
    (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit"
    (func $proc_exit (param i32)))

  (memory (export "memory") 1)
  (data (i32.const 0)  "@\00\00\00")    ;; read buf ptr  = 64
  (data (i32.const 4)  "\00\10\00\00")  ;; read buf len  = 4096
  (data (i32.const 8)  "@\00\00\00")    ;; write buf ptr = 64

  (func (export "_start")
    (local $n i32)
    (local $i i32)
    (local $b i32)
    (local $ret i32)

    (block $done
      (loop $pump
        ;; n = fd_read(0, iov@0, 1, nread@16); ignore errno, use nread
        i32.const 0 i32.const 0 i32.const 1 i32.const 16
        call $fd_read drop
        i32.const 16 i32.load local.set $n
        local.get $n i32.eqz br_if $done

        ;; uppercase transform over buffer[64..64+n)
        i32.const 0 local.set $i
        (block $udone
          (loop $ueach
            local.get $i local.get $n i32.ge_u br_if $udone

            i32.const 64 local.get $i i32.add
            i32.load8_u local.set $b

            ;; if 'a' <= b <= 'z': b -= 0x20
            block $keep
              local.get $b i32.const 0x61 i32.lt_u br_if $keep
              local.get $b i32.const 0x7a i32.gt_u br_if $keep
              local.get $b i32.const 0x20 i32.sub local.set $b
            end

            i32.const 64 local.get $i i32.add
            local.get $b i32.store8

            local.get $i i32.const 1 i32.add local.set $i
            br $ueach))

        ;; write iovec length at offset 12 = n
        i32.const 12 local.get $n i32.store
        i32.const 1 i32.const 8 i32.const 1 i32.const 24
        call $fd_write local.set $ret
        local.get $ret br_if $done   ;; nonzero errno: stop
        br $pump))

    i32.const 0 call $proc_exit)
)

;; Controlled-query demo program.
;; Reads a device model id from stdin (UTF-8, trailing whitespace trimmed),
;; queries the host source "device_specs" with an intentionally small initial
;; buffer, then pulls the complete JSON body via query_read tokens (proving
;; the host does not refetch) and writes it to stdout.
;;
;; Memory layout:
;;   0..7     fd_read iovec  { buf=128, len=512 }
;;   8..15    fd_write iovec { buf=768, len=n }
;;   16..23   nread/nwritten
;;   64..75   source id "device_specs" (12 bytes)
;;   128..639 stdin key buffer
;;   700..707 initial query buffer (8 bytes; forces buffer-too-small)
;;   768..799 read chunk buffer (32 bytes)
(module
  (import "wasi_snapshot_preview1" "fd_read"
    (func $fd_read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write"
    (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit"
    (func $proc_exit (param i32)))
  (import "sandbox_queries" "query_fetch"
    (func $query_fetch (param i32 i32 i32 i32 i32 i32) (result i64)))
  (import "sandbox_queries" "query_read"
    (func $query_read (param i32 i32 i32 i32) (result i64)))

  (memory (export "memory") 1)
  (data (i32.const 0)  "\80\00\00\00")   ;; read buf ptr  = 128
  (data (i32.const 4)  "\00\02\00\00")   ;; read buf len  = 512
  (data (i32.const 8)  "\00\03\00\00")   ;; write buf ptr = 768
  (data (i32.const 64) "device_specs")

  (func $is_space (param $c i32) (result i32)
    (local $r i32)
    local.get $c i32.const 32 i32.eq local.set $r
    local.get $c i32.const 10 i32.eq local.get $r i32.or local.set $r
    local.get $c i32.const 13 i32.eq local.get $r i32.or local.set $r
    local.get $c i32.const 9  i32.eq local.get $r i32.or)

  (func (export "_start")
    (local $n i32) (local $len i32) (local $b i32)
    (local $res i64) (local $st i64) (local $token i32)
    (local $off i32) (local $ret i32)

    ;; Pump stdin into key buffer at 128.
    (block $rdone
      (loop $rpump
        i32.const 0 i32.const 0 i32.const 1 i32.const 16
        call $fd_read drop
        i32.const 16 i32.load local.set $n
        local.get $n i32.eqz br_if $rdone
        local.get $len local.get $n i32.add local.set $len
        ;; shift next iovec pointer (first read only in demo: update ptr)
        i32.const 0 local.get $len i32.const 128 i32.add i32.store
        br $rpump))

    ;; Trim trailing whitespace.
    (block $tdone
      (loop $ttrim
        local.get $len i32.eqz br_if $tdone
        i32.const 128 local.get $len i32.add i32.const 4294967295 i32.add
        i32.load8_u local.set $b
        local.get $b call $is_space i32.eqz br_if $tdone
        local.get $len i32.const 1 i32.sub local.set $len
        br $ttrim))

    ;; fetch with deliberately tiny 8-byte buffer at 700
    i32.const 64 i32.const 12
    i32.const 128 local.get $len
    i32.const 700 i32.const 8
    call $query_fetch local.set $res
    ;; status = high 32 bits
    local.get $res i64.const 32 i64.shr_u local.set $st
    ;; status 3 = buffer too small -> token in low 32 bits
    local.get $st i64.const 3 i64.eq
    (if
      (then
        local.get $res i32.wrap_i64 local.set $token
        (block $cdone
          (loop $cpump
            local.get $token local.get $off
            i32.const 768 i32.const 32
            call $query_read local.set $res
            local.get $res i64.const 32 i64.shr_u local.set $st
            local.get $st i64.eqz i32.eqz br_if $cdone
            local.get $res i32.wrap_i64 local.set $n
            local.get $n i32.eqz br_if $cdone
            i32.const 12 local.get $n i32.store       ;; write iovec len
            i32.const 1 i32.const 8 i32.const 1 i32.const 24
            call $fd_write local.set $ret
            local.get $ret br_if $cdone
            local.get $off local.get $n i32.add local.set $off
            local.get $n i32.const 32 i32.lt_u br_if $cdone
            br $cpump)))
      (else
        ;; STATUS_OK (body <= 8 bytes) would sit at 700; errors exit 20+status.
        local.get $st i64.eqz
        (if
          (then
            ;; fetch length lives nowhere needed; nothing in demo is that small,
            ;; but copy bytes from 700 using the low32 value.
            local.get $res i32.wrap_i64 local.set $n
            i32.const 12 local.get $n i32.store
            i32.const 0 i32.const 700 i32.store  ;; iovec buf = 700
            i32.const 1 i32.const 8 i32.const 1 i32.const 24
            call $fd_write drop
            i32.const 0 i32.const 768 i32.store) ;; restore write iovec ptr
          (else
            local.get $st i32.wrap_i64 i32.const 20 i32.add
            call $proc_exit))))

    ;; trailing newline
    i32.const 800 i32.const 10 i32.store8
    i32.const 12 i32.const 1 i32.store
    i32.const 8 i32.const 800 i32.store
    i32.const 1 i32.const 8 i32.const 1 i32.const 24
    call $fd_write drop
    i32.const 0 call $proc_exit)
)

;; Data-transformation WASI preview1 program that uses the controlled
;; "sandbox" host calls to complete device records with model specs.
;;
;; stdin : one device model per line, e.g.  "TH-100\nAX-7\n"
;; stdout: for each line "<model>\t<spec-body-from-source>\n"; a query
;;         failure yields "<model>\tQUERY_FAILED:<code>\n" and exit code 1.
(module
  (import "wasi_snapshot_preview1" "fd_read"
    (func $fd_read (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write"
    (func $fd_write (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit"
    (func $proc_exit (param i32)))
  (import "sandbox" "query_fetch"
    (func $query_fetch (param i32 i32 i32 i32 i32) (result i32)))
  (import "sandbox" "query_body"
    (func $query_body (param i32 i32 i32 i32) (result i32)))

  (memory (export "memory") 1)
  (data (i32.const 0) "\00P\00\00\00 \00\00")
  (data (i32.const 1024) "specs")
  (data (i32.const 1032) "\09")
  (data (i32.const 1033) "\0a")
  (data (i32.const 1040) "QUERY_FAILED:")

  (func $write_all (param $ptr i32) (param $len i32)
    i32.const 8 local.get $ptr i32.store
    i32.const 12 local.get $len i32.store
    i32.const 1 i32.const 8 i32.const 1 i32.const 24
    call $fd_write drop)

  (func $emit_code_digit (param $code i32)
    i32.const 1056 local.get $code i32.const 48 i32.add i32.store8
    i32.const 1056 i32.const 1 call $write_all)

  (func $fail_line (param $rc i32)
    i32.const 1040 i32.const 13 call $write_all
    local.get $rc call $emit_code_digit
    i32.const 1033 i32.const 1 call $write_all)

  (func $process_line (param $start i32) (param $klen i32) (result i32)
    (local $rc i32) (local $i i32)
    i32.const 0 local.set $i
    (block $cp_done (loop $cp
      local.get $i local.get $klen i32.ge_u br_if $cp_done
      i32.const 2048 local.get $i i32.add
      i32.const 20480 local.get $start local.get $i i32.add i32.add
      i32.load8_u i32.store8
      local.get $i i32.const 1 i32.add local.set $i
      br $cp))

    i32.const 2048 local.get $klen call $write_all
    i32.const 1032 i32.const 1 call $write_all

    i32.const 1024 i32.const 5
    i32.const 2048 local.get $klen i32.const 32
    call $query_fetch local.set $rc
    local.get $rc if
      local.get $rc call $fail_line local.get $rc return
    end

    i32.const 32 i32.load i32.const 4096 i32.const 16384 i32.const 36
    call $query_body local.set $rc
    local.get $rc if
      local.get $rc call $fail_line local.get $rc return
    end

    i32.const 4096 i32.const 36 i32.load call $write_all
    i32.const 1033 i32.const 1 call $write_all
    i32.const 0)

  (func (export "_start")
    (local $n i32) (local $i i32) (local $line_start i32)
    (local $failed i32) (local $rc i32)
    (block $done
      (loop $pump
        i32.const 0 i32.const 0 i32.const 1 i32.const 16
        call $fd_read drop
        i32.const 16 i32.load local.set $n
        local.get $n i32.eqz br_if $done

        i32.const 0 local.set $line_start
        i32.const 0 local.set $i
        (loop $scan
          local.get $i local.get $n i32.ge_u
          if
            local.get $i local.get $line_start i32.gt_u
            if
              local.get $line_start local.get $i local.get $line_start i32.sub
              call $process_line local.set $rc
              local.get $rc if i32.const 1 local.set $failed end
            end
            br $pump
          end
          i32.const 20480 local.get $i i32.add i32.load8_u
          i32.const 10 i32.eq
          if
            local.get $line_start local.get $i local.get $line_start i32.sub
            call $process_line local.set $rc
            local.get $rc if i32.const 1 local.set $failed end
            local.get $i i32.const 1 i32.add local.set $line_start
          end
          local.get $i i32.const 1 i32.add local.set $i
          br $scan)))

    local.get $failed call $proc_exit)
)

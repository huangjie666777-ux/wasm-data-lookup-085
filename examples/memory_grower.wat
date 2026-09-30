;; Repeatedly grows linear memory: must fail against the memory ceiling.
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))
  (memory (export "memory") 1)
  (func (export "_start")
    (local $ok i32)
    loop $grow
      i32.const 1 memory.grow local.set $ok
      ;; memory.grow returns -1 (0xffffffff unsigned) on failure; loop only
      ;; while the returned previous size is non-negative (i.e. success).
      local.get $ok i32.const 0 i32.ge_s
      if
        br $grow
      end
    end
    ;; Reaching here means growth was rejected; exit with a distinct code.
    i32.const 2 call $proc_exit))

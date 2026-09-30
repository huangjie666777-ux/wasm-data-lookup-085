;; Infinite loop with no output: must be killed by the wall-clock deadline.
(module
  (func (export "_start")
    (local $i i32)
    loop $forever
      local.get $i i32.const 1 i32.add local.set $i
      br $forever
    end))

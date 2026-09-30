;; Reaches an unreachable instruction on purpose to demonstrate trap reporting.
(module
  (memory (export "memory") 1)
  (func (export "_start")
    unreachable))

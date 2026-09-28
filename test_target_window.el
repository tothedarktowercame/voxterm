;;; test_target_window.el --- the cursor's buffer outranks the speaking buffer -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_target_window.el
;; bad case (2026-09-28): speak-only = claude-17, cursor in claude-19's REPL,
;; dictation went to claude-17.
(require 'cl-lib)
(let ((a (get-buffer-create "*claude-repl:claude-17*"))
      (b (get-buffer-create "*claude-repl:claude-19*"))
      (fails 0))
  (cl-flet ((check (label got want)
              (unless (equal got want)
                (setq fails (1+ fails))
                (message "FAIL %s: got %S want %S" label got want))))
    (delete-other-windows)
    (set-window-buffer (selected-window) a)
    (let ((w2 (split-window)))
      (set-window-buffer w2 b)
      (select-window w2))
    (let ((voxterm-speak-only-buffer "*claude-repl:claude-17*")
          (voxterm-pinned-buffer-name nil))
      (check "cursor in claude-19 wins over speaking claude-17"
             (voxterm-target-name) "*claude-repl:claude-19*")
      (with-current-buffer b (setq buffer-read-only t))
      (check "unwritable cursor buffer falls back to speaking buffer"
             (voxterm-target-name) "*claude-repl:claude-17*")
      (with-current-buffer b (setq buffer-read-only nil))
      (let ((voxterm-pinned-buffer-name "*claude-repl:claude-17*"))
        (check "a pin still wins over the cursor"
               (voxterm-target-name) "*claude-repl:claude-17*")))
    (message "target-window: %s" (if (zerop fails) "PASS" (format "%d FAIL" fails)))
    (kill-emacs (if (zerop fails) 0 1))))

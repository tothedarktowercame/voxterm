;;; test_focus_frame.el --- a later keystroke outranks a stale focus report -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_focus_frame.el
;; bad case (2026-11-25): F10's terminal stuck `focused' over mosh; Joe typing
;; in F8; dictation went to F10.
(require 'cl-lib)
(let ((fails 0) (focused-frames '(F10)))
  (cl-letf (((symbol-function 'voxterm--usable-frame-p) (lambda (f) (memq f '(F8 F10))))
            ((symbol-function 'filtered-frame-list)
             (lambda (pred) (seq-filter pred '(F10 F8))))
            ((symbol-function 'frame-list) (lambda () '(F10 F8)))
            ((symbol-function 'frame-focus-state)
             (lambda (f) (and (memq f focused-frames) t))))
    (cl-flet ((check (label want)
                (let ((got (voxterm--focus-frame)))
                  (unless (eq got want)
                    (setq fails (1+ fails))
                    (message "FAIL %s: got %S want %S" label got want)))))
      (clrhash voxterm--focus-log)
      (let ((last-event-frame 'F8))
        (setq voxterm--last-key-time 100)
        ;; F10 never seen gaining focus (stuck before load): keystroke wins.
        (check "keystroke in F8 beats a stale focus report on F10" 'F8)
        ;; F10 gains focus after the keystroke (switched terminal, not typing).
        (puthash 'F10 (cons t 200) voxterm--focus-log)
        (check "a focus-in newer than the keystroke wins" 'F10)
        (setq voxterm--last-key-time 300)
        (check "typing again in F8 takes it back" 'F8))
      (let ((last-event-frame 'F10))
        (check "keystroke in the focused frame: focused frame" 'F10))
      ;; transition stamping: only a change to focused stamps a time
      (clrhash voxterm--focus-log)
      (voxterm--note-focus)
      (let ((t1 (voxterm--focus-time 'F10)))
        (voxterm--note-focus)
        (unless (and (> t1 0) (= t1 (voxterm--focus-time 'F10)) (= 0 (voxterm--focus-time 'F8)))
          (setq fails (1+ fails))
          (message "FAIL stamping: re-noting an unchanged focus must not restamp")))))
  (message "focus-frame: %s" (if (zerop fails) "PASS" (format "%d FAIL" fails)))
  (kill-emacs (if (zerop fails) 0 1)))

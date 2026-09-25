;;; test_join_chunks.el --- dictated chunks join without invented stops -*- lexical-binding: t -*-
;;
;; Each dictated chunk is transcribed on its own, so whisper ends each with a
;; full stop and starts the next with a capital.  The first case is Joe's own
;; turn of 2026-09-25, which arrived as "...tries to punctuate my. Communication".
;; What must survive: the last chunk's stop, an ellipsis, a question mark,
;; proper-noun capitals, and a stop the operator typed before dictating.
;;
;; Run: emacs -Q --batch -l voxterm.el -l test_join_chunks.el

(require 'cl-lib)
(setq voxterm-dictation-marker nil)

(defun tjc--run (chunks &optional prefix)
  (let ((buf (get-buffer-create "*tjc*")))
    (with-current-buffer buf (erase-buffer) (when prefix (insert prefix)))
    (set-window-buffer (selected-window) buf)
    (setq voxterm--last-chunk-end nil)
    (with-current-buffer buf (goto-char (point-max)))
    (dolist (c chunks) (voxterm-insert c))
    (with-current-buffer buf (buffer-string))))

(defvar tjc-cases
  '((("It's all very lovely that Vox term tries to punctuate my." "Communication, but it doesn't look very natural.")
     nil "It's all very lovely that Vox term tries to punctuate my communication, but it doesn't look very natural.")
    (("I handed it over to." "Kimi and then." "I waited.")
     nil "I handed it over to Kimi and then I waited.")
    (("So what now?" "Maybe later.") nil "So what now? Maybe later.")
    (("And then..." "Nothing.") nil "And then... Nothing.")
    ;; typed text before the first chunk is not a dictated chunk: left alone
    (("Next thing.") "Typed sentence." "Typed sentence. Next thing.")))

(let ((bad 0))
  (pcase-dolist (`(,chunks ,prefix ,want) tjc-cases)
    (let ((got (tjc--run chunks prefix)))
      (unless (equal got want)
        (setq bad (1+ bad))
        (message "FAIL\n  got:  %S\n  want: %S" got want))))
  ;; with the switch off, the raw joins come back
  (let ((voxterm-join-chunks nil))
    (unless (equal (tjc--run '("my." "Communication.")) "my. Communication.")
      (setq bad (1+ bad)) (message "FAIL: voxterm-join-chunks nil still joined")))
  (message "%d/%d ok" (- (1+ (length tjc-cases)) bad) (1+ (length tjc-cases)))
  (kill-emacs (if (zerop bad) 0 1)))

;;; test_mic_toggle.el --- Prior toggles VoxTerm microphone -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_mic_toggle.el
(require 'cl-lib)
(let (request)
  (cl-letf (((symbol-function 'url-retrieve)
             (lambda (url callback &rest _)
               (setq request url)
               (with-temp-buffer (funcall callback nil)))))
    (call-interactively (key-binding (kbd "<prior>")))
    (unless (equal request "http://127.0.0.1:8081/mic-toggle")
      (message "FAIL Prior request: %S" request)
      (kill-emacs 1))))
(message "ok")

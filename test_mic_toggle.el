;;; test_mic_toggle.el --- F5 toggles VoxTerm microphone -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_mic_toggle.el
;; F5, not <prior>: Joe 2026-11-19 gave <prior>/<next> to the Arxana
;; incubator's version layers.
(require 'cl-lib)
(let (request)
  (cl-letf (((symbol-function 'url-retrieve)
             (lambda (url callback &rest _)
               (setq request url)
               (with-temp-buffer (funcall callback nil)))))
    (call-interactively (key-binding (kbd "<f5>")))
    (unless (equal request "http://127.0.0.1:8081/mic-toggle")
      (message "FAIL F5 request: %S" request)
      (kill-emacs 1))
    (when (eq (key-binding (kbd "<prior>")) #'voxterm-toggle-microphone)
      (message "FAIL <prior> still toggles the microphone")
      (kill-emacs 1))))
(message "ok")

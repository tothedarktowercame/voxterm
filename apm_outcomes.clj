(require '[clojure.edn :as edn] '[clojure.java.io :as io]
         '[cheshire.core :as json])
(defn read-if-present [dir path]
  (let [f (io/file dir path)]
    (when (.isFile f) (edn/read-string (slurp f)))))
(println
 (json/generate-string
  (into {}
   (for [dir *command-line-args*]
    (let [terminal (read-if-present dir "terminal/frame-terminal.edn")
          bank (read-if-present dir "terminal/problem-bank.edn")
          close (when-not terminal (read-if-present dir "live/close-frame.edn"))
          banked (boolean (and terminal bank
                     (= :frame-terminal (:receipt/type terminal))
                     (string? (:receipt/id terminal))
                     (= :queued-problem-bank (:receipt/type bank))
                     (= (:receipt/id terminal) (:source/terminal-receipt-id bank))
                     (= :solved (:problem/outcome bank))
                     (= :pinned (:solve/pin-status bank))))
          result (:frame/result terminal)]
     [dir {:banked banked :frame_result result
           :problem_outcome (:problem/outcome terminal)
           :end (cond
                  (= :partial result) (str "partial learning frame" (when banked "; solver banked"))
                  (= :closed result) (if banked "closed+banked" "closed; bank unconfirmed")
                  terminal (str "terminal: " (name (or result :unknown)))
                  close (str "closure unconfirmed; guide submitted "
                             (name (or (get-in close [:receipt :receipt/result]) :unknown)))
                  :else nil)}])))))

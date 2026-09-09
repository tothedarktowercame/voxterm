;; Read the queue as EDN: reports nested inside parks cannot identify a park.
(require '[clojure.edn :as edn]
         '[cheshire.core :as json])
(let [state (edn/read-string (slurp (first *command-line-args*)))
      parks (:parked state)]
  (when-not (and (map? state) (or (nil? parks) (sequential? parks)))
    (throw (ex-info "invalid queue park collection" {})))
  (println
   (json/generate-string
    (mapv (fn [park]
            {:frame (:frame/id park) :problem (:problem/id park)
             ;; The phase says whether the PROBLEM is stuck or only the
             ;; learning measurement is: a park at :solve/:verify means no
             ;; proof, a park at :student-attempt-N means the proof is
             ;; certified and the student arm stopped.  f206/m02A06 parked at
             ;; :student-attempt-2 on 2026-09-09 with its solve already landed
             ;; on apm-lean master, and read as an unsolved problem.
             :phase (some-> (:phase park) name)
             :code (some-> (:error/code park) name)})
          (filter #(= :awaiting-decision (:decision/status %)) parks)))))

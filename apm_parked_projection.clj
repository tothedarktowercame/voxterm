;; Read the queue as EDN: reports nested inside parks cannot identify a park.
(require '[clojure.edn :as edn]
         '[cheshire.core :as json])
(let [state (edn/read-string (slurp (first *command-line-args*)))
      parks (:parked state)
      repair (:statement-repair/handoff state)]
  (when-not (and (map? state) (or (nil? parks) (sequential? parks)))
    (throw (ex-info "invalid queue park collection" {})))
  (println
   (json/generate-string
    {:rows
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
           (filter #(= :awaiting-decision (:decision/status %)) parks))
     ;; A refuted statement voids its frame and hands the statement to the
     ;; Guide for one repair; the slot then reruns, or is dropped if the
     ;; repair fails. Only the top-level handoff says that is under way:
     ;; f216/m03J02 (2026-09-10) read as a stopped campaign during its repair.
     :repair
     (when (and (= :voided-slot-awaiting-revision (:status state))
                (map? repair))
       {:frame (:frame/id repair) :problem (:problem/id repair)
        :status (some-> (:dispatch/status repair) name)})})))

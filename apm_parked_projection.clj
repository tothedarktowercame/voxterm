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
             :code (some-> (:error/code park) name)})
          (filter #(= :awaiting-decision (:decision/status %)) parks)))))

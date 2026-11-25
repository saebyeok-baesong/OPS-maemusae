--------------------------- MODULE IncidentPipeline ---------------------------
(***************************************************************************)
(* One AnalyzeJob message for one incident, handled by the worker          *)
(* (app/worker.py process_analyze) and then approved by people             *)
(* (app/rollback.py approve). Each action below is one call in the code    *)
(* that touches Redis, Postgres, Discord, or INFRA; a worker can die       *)
(* between any two of them.                                                *)
(*                                                                         *)
(* Faults the model injects (each costs one unit of MaxFaults):            *)
(*   Crash(w)          worker killed (kill -9, OOM, RedisConnectionError)  *)
(*                     between two calls of process_analyze               *)
(*   HeartbeatLost(w)  holding() can no longer XCLAIM (Redis partition    *)
(*                     of one worker) while the worker keeps running       *)
(*   Plan(w) failure   infra.open_rollback_pr raises (GitHub 5xx, 409)     *)
(*   CrashApprover(a)  approvals process killed inside _merge             *)
(* The catalog of faults and the incidents they reproduce lives in        *)
(* README.md next to this file.                                           *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    Workers, Approvers,
    MaxFaults,
    CrashAfterPost,   \* allow Crash between discord.post and store.set_card (the gap the code comment accepts)
    Partition,        \* allow HeartbeatLost
    PRFails,          \* allow open_rollback_pr to fail
    ApproverCrash,    \* allow CrashApprover
    Mutation          \* "none" or a broken variant of the code, see README.md

VARIABLES
    \* Redis stream (app/core/queue.py)
    delivered,     \* XREADGROUP handed the message out once
    pending,       \* in the consumer group's PEL (not XACKed)
    owner,         \* consumer that holds it
    stale,         \* idle longer than queue_claim_idle_ms, so XAUTOCLAIM may take it
    dlq,           \* copied to the dead letter stream
    \* LangGraph checkpoint in incident DB (app/worker.py run_graph)
    ckpt,          \* "none" | "done" (report written)
    \* incidents row (app/core/store.py)
    dbPR,          \* result.rollback_pr: "none" (never written) | "null" (written as None) | "pr"
    dbCard,        \* card_message_id is set
    \* worker locals inside one process_analyze call
    pc, seenCard, seenPR, newPR,
    \* Discord incident channel
    posts,         \* incident cards posted (success card or failure card)
    buttonPosted,  \* a card with the rollback button was posted
    \* approvals
    apc,           \* approver step
    action,        \* actions row for (incident, "rollback"): "none" | "running" | "done"
    attempts,      \* approvals that got past start_action and ran _merge
    prMerged,      \* INFRA rollback PR merged
    faults

vars == <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, pc, seenCard, seenPR, newPR,
          posts, buttonPosted, apc, action, attempts, prMerged, faults>>

NoOwner == "nobody"
Steps == {"idle", "graph", "setfailed", "failrec", "postfail", "failcard", "deadletter", "getrec", "plan", "setstatus",
          "checkcard", "post", "setcard", "ack"}
ASteps == {"idle", "start", "merge", "finish", "done", "already", "noproposal"}

TypeOK ==
    /\ delivered \in BOOLEAN /\ pending \in BOOLEAN /\ stale \in BOOLEAN /\ dlq \in BOOLEAN
    /\ owner \in Workers \cup {NoOwner}
    /\ ckpt \in {"none", "done"}
    /\ dbPR \in {"none", "null", "pr"} /\ dbCard \in BOOLEAN
    /\ pc \in [Workers -> Steps]
    /\ seenCard \in [Workers -> BOOLEAN] /\ seenPR \in [Workers -> {"none", "null", "pr"}]
    /\ newPR \in [Workers -> {"none", "null", "pr"}]
    /\ posts \in Nat /\ buttonPosted \in BOOLEAN
    /\ apc \in [Approvers -> ASteps] /\ action \in {"none", "running", "done"}
    /\ attempts \in Nat /\ prMerged \in BOOLEAN /\ faults \in 0..MaxFaults

Init ==
    /\ delivered = FALSE /\ pending = FALSE /\ owner = NoOwner /\ stale = FALSE /\ dlq = FALSE
    /\ ckpt = "none" /\ dbPR = "none" /\ dbCard = FALSE
    /\ pc = [w \in Workers |-> "idle"]
    /\ seenCard = [w \in Workers |-> FALSE] /\ seenPR = [w \in Workers |-> "none"] /\ newPR = [w \in Workers |-> "none"]
    /\ posts = 0 /\ buttonPosted = FALSE
    /\ apc = [a \in Approvers |-> "idle"] /\ action = "none" /\ attempts = 0 /\ prMerged = FALSE
    /\ faults = 0

Go(w, s) == pc' = [pc EXCEPT ![w] = s]

---------------------------------------------------------------------------
(* Queue: worker.py run_forever, queue.py consume / reclaim_stale *)

Consume(w) ==   \* queue.consume -> XREADGROUP ">"
    /\ pc[w] = "idle" /\ ~delivered
    /\ delivered' = TRUE /\ pending' = TRUE /\ owner' = w /\ stale' = FALSE
    /\ Go(w, "graph")
    /\ UNCHANGED <<dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

Reclaim(w) ==   \* queue.reclaim_stale -> XAUTOCLAIM min_idle_ms
    /\ pc[w] = "idle" /\ pending /\ stale
    /\ owner' = w /\ stale' = FALSE
    /\ Go(w, "graph")
    /\ UNCHANGED <<delivered, pending, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

OwnerGone ==    \* nobody runs holding() for the message any more, so its idle time grows
    /\ pending /\ ~stale /\ pc[owner] = "idle"
    /\ stale' = TRUE
    /\ UNCHANGED <<delivered, pending, owner, dlq, ckpt, dbPR, dbCard, pc, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

HeartbeatLost(w) ==   \* FAULT: holding() XCLAIM fails for longer than queue_claim_idle_ms; the worker goes on
    /\ Partition /\ faults < MaxFaults
    /\ pending /\ ~stale /\ owner = w /\ pc[w] # "idle"
    /\ stale' = TRUE /\ faults' = faults + 1
    /\ UNCHANGED <<delivered, pending, owner, dlq, ckpt, dbPR, dbCard, pc, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged>>

---------------------------------------------------------------------------
(* Worker: worker.py process_analyze, one action per external call *)

Graph(w) ==   \* run_graph: a finished checkpoint returns its values; otherwise the graph may raise
    /\ pc[w] = "graph"
    /\ IF ckpt = "done"
       THEN Go(w, "getrec") /\ UNCHANGED ckpt
       ELSE \/ ckpt' = "done" /\ Go(w, "getrec")
            \/ UNCHANGED ckpt /\ Go(w, "setfailed")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

(* Failure path. The code posts the failure card, then dead_letter.            *)
(* Mutation "dlq_before_post" is the order before the F1 fix: dead_letter first. *)
OldOrder == Mutation = "dlq_before_post"

SetFailed(w) ==   \* store.set_status(..., "failed")
    /\ pc[w] = "setfailed" /\ Go(w, IF OldOrder THEN "deadletter" ELSE "failrec")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

FailRec(w) ==   \* record = store.get(...) in the except branch
    /\ pc[w] = "failrec"
    /\ seenCard' = [seenCard EXCEPT ![w] = dbCard]
    /\ Go(w, "postfail")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

PostFail(w) ==   \* if not record.card_message_id: discord.post(failure card)
    /\ pc[w] = "postfail"
    /\ IF seenCard[w]
       THEN UNCHANGED posts /\ Go(w, IF OldOrder THEN "idle" ELSE "deadletter")
       ELSE posts' = posts + 1 /\ Go(w, "failcard")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, buttonPosted, apc, action, attempts, prMerged, faults>>

FailCard(w) ==   \* store.set_card after the failure card
    /\ pc[w] = "failcard"
    /\ dbCard' = TRUE
    /\ Go(w, IF OldOrder THEN "idle" ELSE "deadletter")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

DeadLetter(w) ==   \* queue.dead_letter: XADD to DLQ, then XACK
    /\ pc[w] = "deadletter"
    /\ dlq' = TRUE /\ pending' = FALSE
    /\ Go(w, IF OldOrder THEN "failrec" ELSE "idle")
    /\ UNCHANGED <<delivered, owner, stale, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

GetRec(w) ==   \* record = store.get(...)
    /\ pc[w] = "getrec"
    /\ seenCard' = [seenCard EXCEPT ![w] = dbCard] /\ seenPR' = [seenPR EXCEPT ![w] = dbPR]
    /\ Go(w, "plan")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

Plan(w) ==   \* plan_rollback(c, job, record.result)
    /\ pc[w] = "plan"
    /\ IF seenPR[w] = "pr" /\ Mutation # "no_pr_reuse"
       THEN newPR' = [newPR EXCEPT ![w] = "pr"] /\ UNCHANGED faults
       ELSE \/ newPR' = [newPR EXCEPT ![w] = "pr"] /\ UNCHANGED faults          \* open_pr (same branch -> same PR)
            \/ /\ PRFails /\ faults < MaxFaults                                 \* FAULT: open_pr raised -> pr None
               /\ newPR' = [newPR EXCEPT ![w] = "null"] /\ faults' = faults + 1
    /\ Go(w, "setstatus")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, posts, buttonPosted, apc, action, attempts, prMerged>>

SetStatus(w) ==   \* store.set_status(..., rollback_pr=pr or None): result || jsonb overwrites the key
    /\ pc[w] = "setstatus"
    /\ dbPR' = newPR[w]
    /\ Go(w, "checkcard")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

CheckCard(w) ==   \* if not record.card_message_id  (record was read in GetRec)
    /\ pc[w] = "checkcard"
    /\ IF seenCard[w] /\ Mutation # "no_card_check" THEN Go(w, "ack") ELSE Go(w, "post")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

Post(w) ==   \* discord.post(incident_card(..., pr)): the button is there when pr is not None
    /\ pc[w] = "post"
    /\ posts' = posts + 1
    /\ buttonPosted' = (buttonPosted \/ newPR[w] = "pr")
    /\ Go(w, "setcard")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, apc, action, attempts, prMerged, faults>>

SetCard(w) ==   \* store.set_card
    /\ pc[w] = "setcard"
    /\ dbCard' = TRUE
    /\ Go(w, "ack")
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, ckpt, dbPR, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

Ack(w) ==   \* queue.ack: XACK works whoever owns the message now
    /\ pc[w] = "ack"
    /\ pending' = FALSE
    /\ Go(w, "idle")
    /\ UNCHANGED <<delivered, owner, stale, dlq, ckpt, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged, faults>>

Crash(w) ==   \* FAULT: the worker dies between two calls. The graph may have finished before it died
    /\ pc[w] # "idle" /\ faults < MaxFaults
    /\ CrashAfterPost \/ pc[w] \notin {"setcard", "failcard"}
    /\ Go(w, "idle") /\ faults' = faults + 1
    /\ IF pc[w] = "graph" THEN ckpt' \in {ckpt, "done"} ELSE UNCHANGED ckpt
    /\ UNCHANGED <<delivered, pending, owner, stale, dlq, dbPR, dbCard, seenCard, seenPR, newPR, posts, buttonPosted, apc, action, attempts, prMerged>>

---------------------------------------------------------------------------
(* Approvals: rollback.py approve and _merge. A click needs the card with the button *)

AV == <<delivered, pending, owner, stale, dlq, ckpt, dbPR, dbCard, pc, seenCard, seenPR, newPR, posts, buttonPosted>>

Click(a) ==   \* record.result.get("rollback_pr") is read at click time
    /\ apc[a] = "idle" /\ buttonPosted
    /\ apc' = [apc EXCEPT ![a] = IF dbPR = "pr" THEN "start" ELSE "noproposal"]
    /\ UNCHANGED <<action, attempts, prMerged, faults>> /\ UNCHANGED AV

Start(a) ==   \* store.start_action: INSERT ... ON CONFLICT (incident_id, action) DO NOTHING
    /\ apc[a] = "start"
    /\ IF action = "none" \/ Mutation = "no_unique_action"
       THEN action' = "running" /\ attempts' = attempts + 1 /\ apc' = [apc EXCEPT ![a] = "merge"]
       ELSE UNCHANGED <<action, attempts>> /\ apc' = [apc EXCEPT ![a] = "already"]
    /\ UNCHANGED <<prMerged, faults>> /\ UNCHANGED AV

Merge(a) ==   \* _merge: image/PR checks, then infra.merge (GitHub refuses a merged PR)
    /\ apc[a] = "merge"
    /\ prMerged' = TRUE
    /\ apc' = [apc EXCEPT ![a] = "finish"]
    /\ UNCHANGED <<action, attempts, faults>> /\ UNCHANGED AV

Finish(a) ==   \* store.finish_action
    /\ apc[a] = "finish"
    /\ action' = "done" /\ apc' = [apc EXCEPT ![a] = "done"]
    /\ UNCHANGED <<attempts, prMerged, faults>> /\ UNCHANGED AV

CrashApprover(a) ==   \* FAULT: approvals killed after start_action, before finish_action; the person clicks again
    /\ ApproverCrash /\ faults < MaxFaults
    /\ apc[a] \in {"merge", "finish"}
    /\ apc' = [apc EXCEPT ![a] = "idle"] /\ faults' = faults + 1
    /\ UNCHANGED <<action, attempts, prMerged>> /\ UNCHANGED AV

---------------------------------------------------------------------------

Next ==
    \/ OwnerGone
    \/ \E w \in Workers :
        \/ Consume(w) \/ Reclaim(w) \/ HeartbeatLost(w) \/ Crash(w)
        \/ Graph(w) \/ SetFailed(w) \/ FailRec(w) \/ PostFail(w) \/ FailCard(w) \/ DeadLetter(w)
        \/ GetRec(w) \/ Plan(w) \/ SetStatus(w) \/ CheckCard(w) \/ Post(w) \/ SetCard(w) \/ Ack(w)
    \/ \E a \in Approvers : Click(a) \/ Start(a) \/ Merge(a) \/ Finish(a) \/ CrashApprover(a)

Spec == Init /\ [][Next]_vars

---------------------------------------------------------------------------
(* Properties. README.md "지키는 것" states each of them *)

\* Discord gets one incident card per incident.
CardOnce == posts <= 1

\* Once the message is XACKed nobody will process it again, so a card must exist or be about to go out.
NoSilentDrop ==
    (delivered /\ ~pending) => (posts >= 1 \/ \E w \in Workers : pc[w] \in {"failrec", "postfail", "post"})

\* A rollback button on Discord points at an incident row that still has rollback_pr.
ButtonWorks == buttonPosted => dbPR = "pr"

\* One rollback _merge per incident, even with two clicks at once.
ExecuteOnce == attempts <= 1

\* An actions row in 'running' belongs to an approval that is still running.
NoStuckApproval == action = "running" => \E a \in Approvers : apc[a] \in {"merge", "finish"}

=============================================================================

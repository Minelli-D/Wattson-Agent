---
name: k8s-investigation
description: Investigate a Kubernetes problem read-only and report the root cause with the evidence that proves it. Use when asked why a pod, rollout, node, volume, certificate, Ingress, Helm release or Flux delivery misbehaves, or whether a workload is healthy. Never changes a production cluster; a needed write becomes a runbook for the user.
---

# k8s-investigation: find the root cause, read-only

Work like this on any Kubernetes question. If a Wattson procedure came with the task, follow its steps; this skill adds the rules and the habits that hold for all of them.

## 1. The rules

- **Read-only first, and read-only on production.** Allowed without asking: `kubectl get`, `describe`, `logs` (also `--previous`), `events`, `top`, `rollout status`, `rollout history`, `auth can-i`, `config get-contexts`; `helm list`, `status`, `history`, `get values` (keys only); `flux get ...`, `flux diff`; `git log`, `show`, `diff`.
- **Everything else is a write:** apply, patch, edit, delete, scale, rollout restart or undo, cordon, drain, label, taint, exec, `kubectl debug node`, `helm upgrade` or rollback, `flux reconcile`, suspend or resume, a push to a GitOps repo. On production you never run one. You write it into a runbook for the user (section 4).
- **Secrets:** never read a Secret's data, never print a token, password, key or kubeconfig. Show only key names.
- **Stay in scope:** the cluster and namespace you were given. Ask before you look anywhere else.

## 2. Which cluster, and how you get in

- `kubectl config get-contexts` and `kubectl config current-context`. A nickname the user says is not always a context name: match it, do not guess.
- Prefer the read-only kubeconfig `wattson-reader` (`~/.config/wattson/kube/<context>.yaml`, used with `--kubeconfig`). It has the built-in role `view` plus get/list/watch on nodes: no Secrets, no exec, no writes. A refusal there is expected, not a bug.
- `kubectl auth can-i --list -n <namespace>` tells what this kubeconfig may do.
- A private service has no public Ingress: reach it with `kubectl -n <namespace> port-forward svc/<service> <local-port>:<port>`.
- `Unauthorized`, `x509: certificate has expired` or a timeout: the problem is access, not the app. Report that first.

## 3. The evidence patterns you can trust

| Symptom | Read | What proves it |
|---|---|---|
| Pod restarts | `describe pod`: Last State | `OOMKilled` (exit 137): memory limit. `Error` + `logs --previous`: the app. Reason `Unknown`, exit 255, several pods on several nodes in the same minute: the node went down, not the app |
| New pods never ready | `rollout status --watch=false`, `get rs`, `describe pod` Events | `0/N nodes are available` (CPU, taint, nodeSelector, PVC), pull errors, `Readiness probe failed` |
| ImagePullBackOff on an old, unchanged image | the event's image; the registry's answer to a HEAD on the manifest | 404 or 401 upstream, the image only in some nodes' cache |
| Terminating for days | `deletionTimestamp`, finalizers, volume unmount errors | kubelet `UnmountVolume failed` lines on the node |
| Change in Git not live | `flux get sources git -A`, `flux get kustomizations -A` | `lastAppliedRevision` older than `lastAttemptedRevision`; one failing object blocks the whole apply |
| Manual change goes back | `get ... --show-managed-fields -o yaml` | the field owner is Flux or Helm |
| Certificate | `get certificate,challenge -n <ns>`, `openssl s_client -servername <host>` | "Kubernetes Ingress Controller Fake Certificate": no real certificate issued yet |
| Node | `get nodes -o wide`, `describe node`, `top nodes` | Conditions, Taints, Allocated resources (requests, not real use) |

- Recent warnings: `kubectl -n <namespace> get events --field-selector type=Warning --sort-by=.lastTimestamp`.
- Read only the tail of big logs (`--tail`, `--since`). Never leave a long command running inside a production pod.
- Put each time in UTC, and compare times across pods and nodes before you name a cause.

## 4. How you report

1. **Confirmed findings:** each one with the command you ran and the lines that prove it.
2. **The root cause**, or the possible causes in order when it is not certain. Say what you did not check.
3. **The fix as a plan:** numbered steps, each with a verify command. Nothing applied.
4. **Open questions:** what would confirm what is still a guess.

A fix that writes to production is a **runbook for someone else**: exact commands with the names filled in, the checks to run first, the verify command after each step, and how to undo it. Mark the risky steps (restarts, deletes, anything on the control plane). The user runs it.

## 5. Facts about the clusters you will meet

- **No default StorageClass.** A PVC without `storageClassName` stays Pending; name the class.
- **Ingress objects are applied by hand,** outside the Flux kustomization, because the ingress-nginx admission webhook often times out (`failed calling webhook ... context deadline exceeded`). A retry usually passes; the real fix is for the cluster admin.
- **Apps are managed by Flux.** A live `kubectl` change is reverted at the next reconcile, so the fix goes into Git. Check which repo and branch the cluster really reads.
- **Small node disks** (about 50 GB root). Pods are evicted under 15 % free, images are cleaned at 85 % use, and logs fill a disk in days. Check disk before you blame the app.
- **A control plane without a taint** also runs app pods.
- **Images removed upstream** may still run from some nodes' caches; moving those pods breaks them.

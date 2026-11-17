# The `wattson` command

`wattson` is the command line of Wattson Agent, your engineering agent. You write a request.
The agent (on Kubernetes) asks questions, asks for evidence from your laptop, and writes a
prompt for Claude Code. You read the prompt, change it if you want, and approve it. Only then
does Claude Code get it, on your laptop, in your terminal.

Its files are in `~/.config/wattson` (settings, token, read-only kubeconfigs) and
`~/.local/state/wattson` (the local task records). Used it before 23 Oct 2026, under its old
name? Then the first command copies the old folders there, once, and says so in one line; the
old folders are kept. The whole move is in [`running.md`](../running.md).

Nothing runs without your yes:

- A risky request (for example "delete the pods") first gets the Guard's question: "Go on with a
  read-only investigation? [y/N]". The default is no. This question is never skipped.
- Evidence (git and kubectl commands) runs only after you answer "Collect this?" with yes.
- The prompt goes to Claude Code only after you type the word `yes`. With `"prompts": "quick"` in `config.json`, a single `y` also approves; the full prompt is still shown first. Quick also shows a one-line summary and a short menu, and skips the approval explanation.
- Claude Code still asks its own permission questions, as usual.

## First time

1. Deploy the agent with `k8s/deploy.sh`. It writes two files for the CLI:
   - `~/.config/wattson/config.json`: the cluster context, the namespace, the deployment name, the port.
   - `~/.config/wattson/token`: the secret token. Its mode must be 600 (only you can read it).
     The CLI refuses a token file that other users can read.
2. Give Wattson Agent read-only access to each cluster you want to investigate:

   ```
   wattson k8s add my-cluster
   ```

   It shows what it will create (a service account bound to the built-in `view` role: it can read,
   it cannot change anything, it cannot read Secrets) and asks first. The read-only kubeconfig goes
   to `~/.config/wattson/kube/my-cluster.yaml` (mode 600).
3. Log the agent in to Grimoire (your knowledge base):

   ```
   wattson grimoire connect
   ```

   It opens the Grimoire login page. If it opens in the wrong browser, copy the link into your
   enrolled Chrome. When the page says "You can close this tab", you are done.
4. Check that everything answers:

   ```
   wattson status
   ```

## Every day

```
cd ~/projects/shop-api
wattson ask "why does deployment/api restart in namespace web?"
```

What happens:

1. **What will be used.** The CLI shows the repository, the branch and your kube contexts. Only
   these names go to the agent at this point. Use `--repo PATH` to pick another repository.
   - **The Guard** checks each request first (see "The Guard" below). Most requests pass without
     a word. A risky request gets a question; a malicious one may be stopped at once.
2. **Questions.** The agent may ask a few things (for example: which namespace?). For a list, type
   the number. Type `cancel` to stop the task.
3. **Evidence.** The agent shows a plan: each command, and why. Items your laptop does not allow
   are marked `REFUSED` and do not run (for example a cluster without read-only access, or
   Secrets). Answer `y` to run the plan, `n` to go on without it.
4. **The proposed prompt.** You see the classification, the Guard's verdict, the target, the
   warnings, the Grimoire sources, the evidence and the missing information. Then a menu:

   | Key | Does |
   |---|---|
   | `p` | read the full prompt |
   | `e` | edit the prompt in your editor (`$VISUAL`, `$EDITOR`, else nano or vi) |
   | `s` | the sources in detail (title, key, why, live or cached, excerpt) |
   | `v` | the evidence in detail (the full output collected on this laptop) |
   | `m` | answer the missing information; the agent writes the prompt again (your edits are lost) |
   | `a` | approve: you type `yes`, then you pick the Claude Code conversation |
   | `r` | reject (you can say why); nothing goes to Claude Code |
   | `q` | quit and leave the task open; it expires after 120 minutes |

5. **The conversation.** With `--session ID` or `--new-session` there is no question. Otherwise
   the CLI lists the Claude Code conversations of this repository: pick one, or start a new one.
   If a conversation was used in the last 2 minutes, it may be open in another window. Continuing
   it starts a second Claude Code process on the same conversation. The CLI offers a copy (fork)
   instead.
6. **The run.** By default Claude Code opens in your terminal. It asks its own permission
   questions. When you leave it, the CLI reads its last answer and reports it to the agent.
   With `--headless`, Claude Code runs without a window (`claude -p`) for at most `--timeout`
   minutes.
7. **The result.** The CLI prints the status, the summary, the sources and the warnings. A task
   the Guard stopped ends with a red line: `Stopped by the Guard: <why>`. Nothing ran.

The CLI checks that the text Claude Code gets is exactly the text you approved (a SHA-256 hash).
If the agent names another text, the CLI refuses and sends nothing.

### The Guard

The agent has a Guard. It looks at each request, and at the evidence and the Grimoire documents
before they go into the prompt. It gives the request one label:

| Label | Means |
|---|---|
| `work` | a normal engineering task |
| `out_of_scope` | not engineering work |
| `risky` | it asks to change or delete something |
| `malicious` | it looks like an attack or abuse |

It decides with fixed rules first, then (if the agent has one) with the small model. The model
can only make the label stricter. The Guard has a mode, set on the agent (`GUARD_MODE`):
`enforce` (it can stop tasks), `monitor` (it only records, it blocks nothing) or `off`.

**The Guard's question.** For a risky request the CLI shows:

```
The Guard has a question
Task           task-d28f1f5e
Request looks  RISKY (it asks to change or delete something)
How decided    decided by fixed rules
Mode           enforce: it can stop tasks
╭─ Why ──────────────────────────╮
│ - It asks to delete pods.      │
╰────────────────────────────────╯
This asks to delete pods. Go on with a read-only investigation?
Go on with a read-only investigation? [y/N]
```

- `y`: the task goes on, read-only only: "Going on, read-only only."
- `n`, Enter, Ctrl-C or the end of input: no. "Stopped. Nothing ran. The task is marked as
  blocked." The exit code is 3.
- The question is never skipped: not by `"prompts": "quick"`, not by `--yes-evidence`.

**At the review** the summary has a `Guard` line: the label, how it was decided, the mode, and
how many evidence or document parts were removed as possible prompt injection, for example:

```
Guard  work (a normal engineering task), decided by fixed rules and the small model, model granite-4-2-3b, mode monitor: it only records, it blocks nothing
```

A `risky` or `malicious` label is also shown in a red `Guard` box with the reasons, at the top.
Removed parts also get a warning line. The quick preset shows the short form
(`Guard: risky (decided by fixed rules, monitor)`) and the same box and warning.

**Stopped by the Guard.** A task the Guard stopped has the status `blocked`. `ask` ends with
`Stopped by the Guard: <why>` (exit code 3); `wattson tasks` shows `blocked (stopped by the
Guard)`; `wattson show TASK` shows the same red line and the reason.

### `ask` options

| Option | Meaning |
|---|---|
| `--repo PATH` | use this git repository instead of the current folder |
| `--cluster CTX` | the kube context to investigate |
| `--namespace NS` | the namespace to investigate; for several, repeat it or use commas: `--namespace grimoire,kubeai` (at most 5) |
| `-A`, `--all-namespaces` | investigate every namespace of the cluster (not with `--namespace`) |
| `--workload KIND/NAME` | the workload, for example `deployment/api` |
| `--workflow W` | skip the classification: `prompt-optimization`, `devops-investigation` or `engineering-task` |
| `--session ID` | continue this Claude Code conversation (see `wattson sessions`) |
| `--new-session` | start a new Claude Code conversation |
| `--fork` | continue the conversation in a copy |
| `--headless` | run Claude Code without a window |
| `--timeout MIN` | the limit of a headless run, in minutes (default 30) |
| `--use-my-kubeconfig` | allow kubectl with your own kubeconfig for a cluster without read-only access |
| `--yes-evidence` | collect the evidence without asking |

### Several namespaces, or all of them

```
wattson ask "why are the pods not ready?" --cluster my-cluster --namespace grimoire,kubeai
wattson ask "which deployments are not ready?" --cluster my-cluster -A
```

- **One namespace** (or none, and you answer the question with one name): the same as before.
- **Several namespaces** (2 to 5): the plan reads the deployments, the pods and the events of
  each namespace, one item per namespace. More than 5 is refused: use `-A`.
- **All namespaces** (`-A`): one listing of the deployments and one of the pods across the
  cluster, and the newest 50 warning events. A listing keeps the rows with a problem first (not
  ready, a status other than Running or Completed, a restart in the last hour), up to 12 kB; a
  last line says how many rows were cut.
- **A workload** (`--workload deployment/api`) with several namespaces or `-A`: describe,
  rollout and logs need one namespace. After the first round, the agent finds where the listing
  shows the workload and asks for a short second round there ("More evidence the agent wants"),
  in at most 3 namespaces. Not found anywhere: a warning, and no second round.
- **Every name is checked.** A name that is not a namespace name stops `ask` at once. With
  `--cluster`, each namespace must exist there, or `ask` says which one does not (exit code 2).
  Without `--cluster`, the plan marks the items of a missing namespace `REFUSED` and they do
  not run.
- **The question** "Which namespace?" takes one name, several with commas, or `*` for all.
- The review shows `namespaces grimoire, kubeai` or `all namespaces`; `wattson tasks` shows
  `my-cluster/{grimoire,kubeai}` or `my-cluster/*`.

## Other commands

| Command | Does |
|---|---|
| `wattson tasks [--limit N]` | the latest tasks on the agent; tasks still open on this laptop are marked |
| `wattson show TASK` | one task: its record and its history (status changes) |
| `wattson recover TASK [--session ID]` | report a Claude Code result that did not reach the agent |
| `wattson sessions [--repo PATH]` | the Claude Code conversations of a repository |
| `wattson status` | the settings, the token file, and the health of the agent, the model and Grimoire |
| `wattson grimoire connect [--port N] [--no-browser]` | log the agent in to Grimoire (callback on `127.0.0.1:47615`) |
| `wattson grimoire status` | is the agent connected to Grimoire? |
| `wattson grimoire disconnect` | the agent forgets its Grimoire login |
| `wattson k8s add CONTEXT [--namespace NS]` | read-only access to a cluster (asks first); the account goes into the namespace of Wattson Agent on its own cluster, or into NS |
| `wattson k8s list` | your kube contexts, and which have read-only access |
| `wattson --version` | the version |

## When something stops in the middle

The CLI keeps the approved prompt and the conversation id in
`~/.local/state/wattson/tasks/<task>.json` (mode 600) until the result is reported. Then the prompt
is deleted from that file.

- If the agent restarted while Claude Code was running, the CLI reports the result with a late
  report. You do not need to do anything.
- If the CLI could not report (no network, Ctrl-C), run `wattson recover TASK`. It reads Claude
  Code's last answer from the conversation file and reports it. `wattson tasks` lists the tasks
  that still need this.
- A Claude Code run is never started again by Wattson Agent.

## Connection

The agent has no public address. For each command, the CLI starts
`kubectl port-forward deploy/wattson-orchestrator` on a free local port (with your own kubeconfig
and the context in `config.json`), and stops it at the end. If it drops during a long run, the CLI
opens it again. Every request carries the token.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | an error (the message says what) |
| 2 | a wrong command or option |
| 3 | you rejected or cancelled the task, or the Guard stopped it (`blocked`) |

## Settings from the environment

| Variable | Meaning |
|---|---|
| `WATTSON_URL` | talk to this address, no port-forward (development), e.g. `http://127.0.0.1:9000` |
| `WATTSON_TOKEN_FILE` | the token file (default `~/.config/wattson/token`) |
| `WATTSON_CONFIG_DIR` | the settings folder (default `~/.config/wattson`) |
| `WATTSON_STATE_DIR` | the local task records (default `~/.local/state/wattson`) |
| `WATTSON_KUBECTL_BIN` | the kubectl program |
| `WATTSON_CLAUDE_BIN` | the claude program |
| `WATTSON_CLAUDE_HOME` | Claude Code's folder (default `~/.claude`) |
| `NO_COLOR` | no colours |

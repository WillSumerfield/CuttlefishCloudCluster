---
title: Dashboard tour
---
The [[Glossary#Dashboard|Dashboard]] is CCC's website, served by the Head. Use it to watch the queue,
browse your files, check your Karma and manage your Tidepool. To open it from a machine with `cuttle`
set up:

```bash
cuttle dashboard
```

Sign in with the same username and password as `cuttle login`. The pages are listed down the left
(along the bottom on a phone). The **Wiki** link under the CCC name brings you here.

Start a Dive from the Queue page or with `cuttle` (see [[Dives]]). Connect to it from your terminal
or editor using the instructions on its Dive page.

## Queue

[Open the Queue](dashboard:/). The landing page: what the cluster is doing right now. It updates every few
seconds.

**Start a Dive** sits beside the Queue (above it on smaller screens). Browse your Tidepool or type
your main folder, then choose a time limit and total VRAM in GB. Existing choices load from that
folder's `cuttle.toml`; your changes are saved when you start. **More options** holds GPU count,
read-only folders, an environment file and Features. After creating it, you go to the Dive page to
watch it become ready and find connection instructions.

- **Running** Dives show who started them, their main folder, how many GPUs and which machine.
- **Waiting** Dives are numbered in the order they'll start, with how long each asked for.
- The top right says how many of the cluster's GPUs are working.
- Your own Dives have an **End** button. Click any Dive to open its page.

## History

[Open History](dashboard:/history). Every finished Dive from the last 30 days, newest first: who ran it, how it
ended, where it ran, and what it cost.

Filter by **Me** or **Everyone**, by outcome (ended, timed out, lost), or by Arm, or search for a
folder, user or Dive number. Hover over an outcome to see what it means. [[Dives]] explains them too.

## Dive page

Click any Dive in the Queue or History to open its page (`/dive/<id>`). It lists who started it, the
main folder, the Arm, GPUs, VRAM, time limit, start and finish times, and its cost.

For your own Dive while it's live, a **Connect** box shows the command to get in
(`cuttle dive attach <id>`), or says it's still queued or copying your folders. An **End** button
sits at the top.

## Arms

[Open Arms](dashboard:/arms). Every machine lent to the cluster, with its owner, GPU memory, Power (how fast
it measured), and its address on the cluster's private network.

If you own an Arm, it's listed under **My arms**, with **Retract** to take it back right away and
**Return** to give it back. See [[Arms]].

## Karma

[Open Karma](dashboard:/karma). Your balance and Escrow, everyone's standing side by side, and your own
history over the last 30 days: a graph of your balance and a list of every Dive that settled. Hover
over any label for a short explanation. See [[Karma]].

Admins see a note instead of a balance, because they're outside Karma.

## Tide

[Open the Tide](dashboard:/tide). How busy the cluster usually is at each hour of the day, and how this hour
compares with a typical one. Busy hours cost more Karma. Below it, who has spent the most Karma
in the last 30 days. See [[Karma#The Tide|The Tide]].

## Files

[Open Files](dashboard:/files). Browse your Tidepool home, upload and download files and folders, make
folders, rename and delete. A bar at the top shows how full your Tidepool is. See [[Files]].

## Account

[Open Account](dashboard:/account). Who you are, and the controls for your Tidepool:

- your Karma and the Arms you own, each linking to its page
- your **Tidepool**: whether it's running, how full it is, the host name for VS Code, and
  **Restart** and **Reset** buttons (see [[Tidepool]])
- **SSH keys** that can log in to your Tidepool: add or remove them
- change your password, and sign out

## Users (Admins only)

[Open Users](dashboard:/users). Every User with their Karma, Escrow, Share and the Arms they've lent. It's
read-only: Admins add Users, reset passwords and adjust Karma with `cuttle` on the Head. See
[[Users and access]].

## Related

- [[Using CCC/index|Using CCC]]: parent
- [[Dives]]: the Queue, History and Dive pages in more depth
- [[Files]]: the Files page
- [[Karma]]: the Karma and Tide pages
- [[Tidepool]]: what Restart and Reset do
- [[Arms]]: the Arms page, for owners
- [[Users and access]]: the Users page, for Admins

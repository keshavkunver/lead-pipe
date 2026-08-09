# Running to-do

- [ ] Parameterize discovery by niche + area from the CLI, e.g.
      `python orchestrator.py discover --niche "basketball trainers" --geo "Orange County / LA, California"`.
      The candidate markets are currently the hardcoded `markets` list in
      `orchestrator.job_discover`; this becomes CLI/config plumbing into that
      list (the MarketBandit already picks among whatever pairs it is given,
      and the `leads` table already carries `niche` and `geo` columns).

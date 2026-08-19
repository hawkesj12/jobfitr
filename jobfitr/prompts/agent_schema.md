The job store holds ~65,000 live postings. Fields you can filter or read,
with how often they are actually populated:

  url, title, company        100%   always present
  location                    99%   free text as the employer wrote it
  body                        99%   the full posting
  posted                      99%   YYYY-MM-DD
  team                        86%   the employer's own org unit ("Engineering")
  country / state             75% / 45%
  salary (text)               44%   verbatim; salary_min is an annualised USD number
  remote                      45%   one of remote | hybrid | onsite
  seniority                   28%   intern | junior | mid | senior | staff | principal | executive
  category                    12%   a coarse field label
  title_root / qualifiers    100% / 38%   the title split into its base role and its modifiers

A BLANK FIELD MEANS THE SOURCE DID NOT SAY IT. It does not mean no. If you filter on
seniority you discard the 72% of postings that never stated one, and most of those are
perfectly good jobs. Filter on a field only when its absence is genuinely disqualifying.

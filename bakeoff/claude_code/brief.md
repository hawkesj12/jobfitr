You extract a job seeker's search preferences from a COMPLETE chat transcript into a structured config. The whole transcript is provided at once, so extract EVERY field the transcript supports in a single pass.

The 8 config fields (include only fields the transcript actually supports; omit any the user never mentioned — never invent a value):
- titles: the roles the user wants (list of strings)
- boosts: signals that should rank a job HIGHER — skills, tools, industry, a nearby city (list)
- exclude: title words that should HIDE a job entirely, e.g. intern/volunteer (list)
- rank_down: signals that should SINK a job but not hide it, e.g. staffing/agency (list)
- location: a place string, or 'remote', or 'anywhere'
- remote_only: boolean — true only if the user wants remote-only roles
- max_age_days: integer — ignore postings older than this many days
- min_score: one of 'plenty', 'balanced', 'strong' (how picky the user is)

Use the user's own words for the list items. Interpret the user's intent yourself; do not expect explicit keywords.

Return ONLY a JSON array (no prose, no code fences). For EACH transcript below output {"id": "<case id>", "config": {<only the supported fields>}}.

=== TRANSCRIPTS ===
--- id: 001-zookeeper ---
Assistant: What kind of role are you looking for?
User: I'm a zookeeper, ideally working with reptiles. Also open to 'animal keeper' roles.
Assistant: Anything that makes a role a better fit?
User: A biology degree is a plus. I really don't want internships or volunteer gigs. And skip anything from staffing agencies.
Assistant: Where, and how fresh?
User: Louisville KY, not remote. Fresh postings only, last month or so. Show me plenty.

--- id: 002-remote-react-dev ---
User: Senior frontend engineer, React and TypeScript. Remote only. I care about design systems and accessibility. No agency or contract-to-hire junk. Only show me the strongest matches.

--- id: 003-vague-career-switcher ---
Assistant: What role are you after?
User: Honestly not sure exactly, something in project management I think.
Assistant: Any location preference?
User: Anywhere is fine.

--- id: 004-voice-rambler ---
User: ok so um i've been a line cook for like six years and i wanna move up to sous chef maybe kitchen manager, uh anything farm to table would be great i love that, please no catering gigs though, and yeah keep it around nashville, last couple weeks of postings, give me a balanced amount not too crazy

--- id: 005-nurse-nightshift ---
User: Registered nurse, ICU or ER. I'd rank travel-nursing agencies way down. Hard no on any 'nurse educator' or 'case manager' postings. Around Denver, within the last 45 days. Balanced is fine.

--- id: 006-federal-analyst ---
User: Looking for a data analyst or program analyst role, ideally federal government. SQL and Tableau are my strengths. Washington DC area. Not remote necessarily.

--- id: 007-trades-electrician ---
User: Journeyman electrician, commercial preferred. Around Phoenix AZ. Don't show apprenticeships.

--- id: 008-remote-anywhere-writer ---
User: Freelance technical writer or content designer, fully remote, work from anywhere. I love developer docs and API documentation. Show me lots of options.

--- id: 009-teacher-relocating ---
User: High school math teacher. We're relocating to Austin, Texas. No substitute or paraprofessional positions please. Recent stuff, past 30 days.

--- id: 010-overloaded-boosts ---
User: DevOps / platform engineer. Kubernetes, Terraform, AWS, CI/CD, observability, Go. Remote only. Rank down anything that's really an SRE-on-call-heavy role. Strong matches only.

--- id: 011-title-only ---
User: barista jobs

--- id: 012-conflicting-remote ---
User: Accountant or bookkeeper roles around Portland Oregon, though I'm open to remote too. Skip any commission-only or 1099 contractor stuff. Balanced picky.

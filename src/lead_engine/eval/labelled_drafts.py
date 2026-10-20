"""40 hand-labelled drafts for measuring the six checks (docs/DESIGN.md sections 7 and 18.1).

`fails` lists the checks a correct implementation must fail. Good drafts have fails=[].
Target: every banned item caught (0 false negatives), at most 10% false alarms on good drafts.
"""

LINK = "https://cal.com/indoko/x"

EV = {
    "founder": ["Northwind raised $4M in a seed round", "Northwind was founded in 2025 in San Francisco"],
    "engineer": ['Patent: "Distributed cache invalidation"', 'Talk: "Scaling caches at Google"'],
    "researcher": ['Paper "Compilers at scale" in PLDI (2026)', "2,500 citations across 40 papers"],
    "creator": ["won the Grand Jury Prize at Sundance", "her documentary Salt Roads premiered in 2026"],
    "warm": ["Title on file: Staff Engineer. Company on file: Stripe"],
}

FU1 = "A short note in case the timing is better now. Happy to answer questions by email as well."
FU2 = "Last note from me. If US options come up later, we're glad to help."


def d(id_, seg, subject, body, fails=(), fu1=FU1, fu2=FU2):
    return {"id": id_, "segment": seg, "subject": subject, "body": body, "fu1": fu1, "fu2": fu2,
            "fails": sorted(fails)}


GOOD = [
    d("g01", "founder", "Northwind's seed round",
      f"Hi Ada,\n\nSaw that Northwind raised $4M in a seed round. Some founders look at long-term US options "
      f"such as the O-1, EB-1A or EB-2 NIW as the company grows. Would a 30-minute consultation be useful? {LINK}\n\nKarishma"),
    d("g02", "founder", "A question after your seed",
      f"Hi Ada,\n\nCongratulations on the seed round for Northwind. When a company is a year old, founders often "
      f"start planning their long-term US options, like the O-1 or EB-1A. If that's on your mind, you can book "
      f"30 minutes with us here: {LINK}\n\nKarishma"),
    d("g03", "founder", "Northwind and long-term plans",
      f"Hi Ada,\n\nNorthwind was founded in 2025 and just raised $4M. Some founders at that stage look at options "
      f"like the O-1, EB-1A or EB-2 NIW. Is a 30-minute call worth it? {LINK}\n\nKarishma"),
    d("g04", "founder", "Your seed round",
      f"Hi Ada,\n\nSaw the news about Northwind's seed. We help founders understand routes like the O-1, EB-1A "
      f"and EB-2 NIW. Happy to talk it through in a 30-minute consultation: {LINK}\n\nKarishma"),
    d("g05", "founder", "Northwind",
      f"Hi Ada,\n\nA $4M seed is a real milestone for Northwind. If you're thinking about your options in the US "
      f"over the next few years, a 30-minute consultation can help. Book here: {LINK}\n\nKarishma"),
    d("g06", "engineer", "Your cache invalidation patent",
      f"Hi Ada,\n\nSaw your patent on distributed cache invalidation. Some engineers with patents and talks look "
      f"at long-term US options such as the O-1, EB-1A or EB-2 NIW. Would a 30-minute consultation help? {LINK}\n\nKarishma"),
    d("g07", "engineer", "Scaling caches at Google",
      f"Hi Ada,\n\nYour talk on scaling caches caught my eye. We help engineers understand routes like the O-1 "
      f"and EB-1A. If it's useful, you can book 30 minutes here: {LINK}\n\nKarishma"),
    d("g08", "engineer", "A note about your talk",
      f"Hi Ada,\n\nRead about your talk, \"Scaling caches at Google\". Some engineers with public work like that "
      f"explore options such as the O-1, EB-1A or EB-2 NIW. Worth a 30-minute call? {LINK}\n\nKarishma"),
    d("g09", "engineer", "Your patent",
      f"Hi Ada,\n\nNoticed the patent on distributed cache invalidation. If long-term US plans are on your mind, "
      f"we offer a 30-minute consultation: {LINK}\n\nKarishma"),
    d("g10", "researcher", "Compilers at scale",
      f"Hi Grace,\n\nRead your PLDI paper, \"Compilers at scale\". Some researchers look at long-term US options "
      f"such as the O-1, EB-1A or EB-2 NIW. Would a 30-minute consultation be useful? {LINK}\n\nKarishma"),
    d("g11", "researcher", "Your PLDI paper",
      f"Hi Grace,\n\nYour work has 2,500 citations across 40 papers. If you're planning your next years in the "
      f"US, a 30-minute consultation can help you see the options. Book here: {LINK}\n\nKarishma"),
    d("g12", "researcher", "A question about your plans",
      f"Hi Grace,\n\nSaw \"Compilers at scale\" in PLDI this year. We help researchers understand routes like "
      f"the O-1 and EB-2 NIW. Is a 30-minute call worth it? {LINK}\n\nKarishma"),
    d("g13", "researcher", "Compilers at scale",
      f"Hi Grace,\n\nYour paper in PLDI stood out. Some researchers in your field look at options such as the "
      f"EB-1A or EB-2 NIW. Happy to talk it through in 30 minutes: {LINK}\n\nKarishma"),
    d("g14", "creator", "Your Sundance prize",
      f"Hi Maya,\n\nCongratulations on the Grand Jury Prize at Sundance. Some filmmakers look at long-term US "
      f"options such as the O-1. Would a 30-minute consultation help? {LINK}\n\nKarishma"),
    d("g15", "creator", "Salt Roads",
      f"Hi Maya,\n\nSaw that Salt Roads premiered this year. We help artists understand routes like the O-1 and "
      f"EB-1A. If it's useful, book 30 minutes here: {LINK}\n\nKarishma"),
    d("g16", "creator", "After Sundance",
      f"Hi Maya,\n\nThe Grand Jury Prize at Sundance is a big moment. If you're thinking about your options in "
      f"the US, a 30-minute consultation can help. Book here: {LINK}\n\nKarishma"),
    d("g17", "warm", "Following up from Indoko",
      f"Hi Sam,\n\nYou signed up with Indoko a while ago. If you're still thinking about the O-1, EB-1A or EB-2 "
      f"NIW, we'd be glad to help in a 30-minute consultation: {LINK}\n\nKarishma"),
    d("g18", "warm", "Still exploring your options?",
      f"Hi Sam,\n\nA quick note from Karishma at Indoko. If US options like the O-1 or EB-1A are on your mind, "
      f"you can book 30 minutes with us: {LINK}\n\nKarishma"),
    d("g19", "warm", "A consultation, if useful",
      f"Hi Sam,\n\nAs a staff engineer at Stripe you may be planning ahead. We offer a 30-minute consultation "
      f"on routes like the O-1 and EB-2 NIW: {LINK}\n\nKarishma"),
    d("g20", "warm", "Indoko",
      f"Hi Sam,\n\nWe're opening a few consultation slots this month. Would 30 minutes on your options in the "
      f"US help? {LINK}\n\nKarishma"),
]

BAD = [
    # grounded: invented names or numbers
    d("b01", "founder", "Northwind's Series B",
      f"Hi Ada,\n\nSaw Northwind raised $40M in a Series B led by Sequoia Capital. Would a 30-minute call help? {LINK}\n\nKarishma",
      ["grounded"]),
    d("b02", "engineer", "Your talk",
      f"Hi Ada,\n\nLoved your keynote at Google Cloud Next about 12 cache layers. Would a 30-minute call help? {LINK}\n\nKarishma",
      ["grounded", "tone"]),
    d("b03", "researcher", "Your Nature paper",
      f"Hi Grace,\n\nYour Nature Machine Intelligence paper has 9,000 citations. Would a 30-minute call help? {LINK}\n\nKarishma",
      ["grounded"]),
    d("b04", "creator", "Your Oscar nomination",
      f"Hi Maya,\n\nCongratulations on the Academy Award nomination. Would a 30-minute call help? {LINK}\n\nKarishma",
      ["grounded"]),
    # eligibility
    d("b05", "founder", "You qualify",
      f"Hi Ada,\n\nWith a $4M seed round you likely qualify for the O-1. Book a 30-minute call: {LINK}\n\nKarishma",
      ["no_eligibility"]),
    d("b06", "engineer", "Your O-1 case",
      f"Hi Ada,\n\nYour patent makes you a perfect fit for the O-1A. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_eligibility"]),
    d("b07", "researcher", "EB-1A",
      f"Hi Grace,\n\nWith 2,500 citations across 40 papers you'd be approved for the EB-1A. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_eligibility"]),
    d("b08", "creator", "Guaranteed",
      f"Hi Maya,\n\nAfter the Grand Jury Prize at Sundance we can guarantee a strong O-1 outcome. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_eligibility"]),
    # sensitive traits
    d("b09", "founder", "Founders like you",
      f"Hi Ada,\n\nAs an Indian founder who raised $4M, the O-1 may matter to you. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_sensitive"]),
    d("b10", "engineer", "H-1B to O-1",
      f"Hi Ada,\n\nIf you're on an H-1B, your patent could help. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_sensitive"]),
    d("b11", "researcher", "International researchers",
      f"Hi Grace,\n\nMany international students publish in PLDI. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_sensitive"]),   # the claim has no name or number, so grounding is the reviewer's job by design
    d("b12", "creator", "Your visa status",
      f"Hi Maya,\n\nGiven your visa status, the Grand Jury Prize at Sundance matters. Book 30 minutes: {LINK}\n\nKarishma",
      ["no_sensitive"]),
    # form
    d("b13", "founder", "Re: Northwind",
      f"Hi Ada,\n\nSaw the seed round. Would a 30-minute consultation help? {LINK}\n\nKarishma", ["form"]),
    d("b14", "engineer", "Your patent",
      f"Hi Ada,\n\nSaw the patent. Book here {LINK} or read more at https://indoko.example/blog\n\nKarishma", ["form"]),
    d("b15", "researcher", "Compilers at scale",
      "Hi Grace,\n\n" + ("Your paper on compilers is relevant to what we do. " * 14) + f"Book: {LINK}\n\nKarishma",
      ["form"]),
    # footer: the email text includes its own opt-out line
    d("b16", "founder", "Northwind's seed round",
      f"Hi Ada,\n\nSaw the seed round. Book 30 minutes: {LINK}\n\nKarishma\n\nReply unsubscribe to stop these emails.",
      ["footer"]),
    d("b17", "creator", "Salt Roads",
      f"Hi Maya,\n\nSaw Salt Roads premiered. Book 30 minutes: {LINK}\n\nTo opt out, just reply.",
      ["footer"]),
    # tone
    d("b18", "founder", "Northwind",
      f"Hi Ada,\n\nI hope this email finds you well. Saw the seed round. Book 30 minutes: {LINK}\n\nKarishma", ["tone"]),
    d("b19", "engineer", "Your patent",
      f"Hi Ada,\n\nI'm reaching out because your patent is impressive. Book 30 minutes: {LINK}\n\nKarishma", ["tone"]),
    d("b20", "researcher", "Your paper",
      f"Hi Grace,\n\nJust checking in about your amazing PLDI paper. Book 30 minutes: {LINK}\n\nKarishma", ["tone"]),
]

ALL = GOOD + BAD
NAMES = {"founder": "Ada Lovelace", "engineer": "Ada Lovelace", "researcher": "Grace Hopper",
         "creator": "Maya Chen", "warm": "Sam Rivera"}

"""Public, persona-free vocabulary for reading a job posting (spec 070).

Nothing here is a fact about the candidate. These lists say how postings
are laid out and which words name a technology or a concept, so the fit
evaluation can tell a requirement from a company blurb and can split
"React + TypeScript / Next.js" into the three things it asks for. The
bundle's own `technology_aliases` extend the technology list at run time.
"""

from __future__ import annotations

import re

SECTION_HEADERS: dict[str, tuple[str, ...]] = {
    "requirement": (
        "requirements",
        "requirement",
        "qualifications",
        "what you bring",
        "what we're looking for",
        "what we are looking for",
        "who you are",
        "your profile",
        "about you",
        "skills",
        "must have",
        "must haves",
        "must-have",
        "must-haves",
        "nice to have",
        "nice to haves",
        "nice-to-have",
        "bonus points",
        "you have",
        "you bring",
        "your skills",
        "experience",
    ),
    "responsibility": (
        "tasks",
        "your tasks",
        "responsibilities",
        "your responsibilities",
        "what you'll do",
        "what you will do",
        "the role",
        "your role",
        "about the role",
        "your mission",
        "in this role",
    ),
    "benefit": (
        "benefits",
        "what we offer",
        "we offer",
        "perks",
        "perks and benefits",
        "why join us",
        "why us",
        "compensation and benefits",
    ),
    "company_context": (
        "about us",
        "who we are",
        "our mission",
        "our story",
        "the company",
        "company",
        "about the company",
        "the team",
    ),
}

# A header naming one of these marks every item under it nice-to-have.
NICE_TO_HAVE_HEADERS = ("nice to have", "nice-to-have", "bonus")

# Invitations and legal notices are company context wherever they sit
# (spec 070 X2).
COMPANY_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blet['\u2019]?s talk\b", re.IGNORECASE),
    re.compile(r"\bif you enjoy\b", re.IGNORECASE),
    re.compile(r"\bjoin us\b", re.IGNORECASE),
    re.compile(r"\bapply now\b", re.IGNORECASE),
    re.compile(r"\bequal opportunity\b", re.IGNORECASE),
    re.compile(r"\bwe(?:['\u2019]d| would) love to hear\b", re.IGNORECASE),
)

# Canonical technology name to the aliases that name it in text. An alias
# that is also an ordinary word ("lambda", "rails") is left out: it would
# make "lambda functions" or "guard rails" evidence for a technology.
TECHNOLOGIES: dict[str, tuple[str, ...]] = {
    "JavaScript": ("javascript",),
    "TypeScript": ("typescript",),
    "React": ("react", "react.js", "reactjs"),
    "React Native": ("react native",),
    "Next.js": ("next.js", "nextjs"),
    "Vue": ("vue", "vue.js", "vuejs"),
    "Nuxt": ("nuxt", "nuxt.js"),
    "Angular": ("angular",),
    "Svelte": ("svelte", "sveltekit"),
    "Node.js": ("node.js", "nodejs", "node js"),
    "Deno": ("deno",),
    "Python": ("python",),
    "Django": ("django",),
    "Ruby on Rails": ("ruby on rails",),
    "Ruby": ("ruby",),
    "Java": ("java",),
    "Kotlin": ("kotlin",),
    "Go": ("golang",),
    "Rust": ("rust",),
    "PHP": ("php",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Elasticsearch": ("elasticsearch",),
    "Kafka": ("kafka",),
    "GraphQL": ("graphql",),
    "REST": ("rest api", "rest apis", "restful"),
    "AWS": ("aws", "amazon web services"),
    "AWS Lambda": ("aws lambda", "amazon lambda"),
    "Amazon S3": ("amazon s3", "aws s3"),
    "Amazon EC2": ("amazon ec2", "aws ec2"),
    "Amazon ECS": ("amazon ecs", "aws ecs"),
    "DynamoDB": ("dynamodb",),
    "CloudFormation": ("cloudformation",),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Terraform": ("terraform",),
    "CI/CD": ("ci/cd", "continuous integration", "continuous delivery"),
    "Storybook": ("storybook",),
    "Playwright": ("playwright",),
    "Cypress": ("cypress",),
    "Jest": ("jest",),
    "Vitest": ("vitest",),
    "Tailwind CSS": ("tailwind", "tailwind css"),
    "Sentry": ("sentry",),
}

# A family is named by the posting as a whole ("AWS"); a member names one
# part of it. A member is Partial evidence for its family, never Direct
# (spec 070 R2).
FAMILIES: dict[str, tuple[str, ...]] = {
    "AWS": ("AWS Lambda", "Amazon S3", "Amazon EC2", "Amazon ECS", "DynamoDB", "CloudFormation"),
}

# Concept terms a requirement can name without naming a technology. The
# bundle's dimension signals are added at run time.
CONCEPTS: tuple[str, ...] = (
    "design patterns",
    "distributed systems",
    "distributed computing",
    "distributed",
    "microservices",
    "event-driven",
    "cloud architecture",
    "cloud",
    "architecture",
    "system design",
    "scalability",
    "performance",
    "observability",
    "monitoring",
    "security",
    "accessibility",
    "agile",
    "scrum",
    "saas",
    "b2b",
    "integrations",
    "design system",
    "testing",
    "test automation",
    "code review",
    "mentoring",
    "technical leadership",
)

# Seniority levels a requested role can name (spec 070 G3).
SENIORITY_LEVELS = ("staff", "principal", "lead", "head")

"""The workflow registry — one declarative source for navigation and workflow metadata.

Three kinds of metadata are kept deliberately separate:

* **Navigation** (`NavigationEntry`) — where an entry appears and what it is called.
* **Workflow** (`WorkflowDefinition`) — the dealer job, its context, and its status.
* **Skill** (`SkillId`) — which reusable capabilities the workflow draws on.

The registry contains **no calculation**. It binds a `WorkflowContext` to an existing
render function and nothing more.

It imports `pricing_agent.views` for those render callables, which is why
`workflows/__init__.py` exports only `context` — importing the registry at package level
would close a cycle with the views that need `WorkflowContext`.

Streamlit is intentionally not imported here. Page objects are built by the entry script,
so the registry stays importable — and testable — without a Streamlit runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Callable

from pricing_agent.views.assistant_home import WorkflowCard, render_assistant_home
from pricing_agent.views.dashboard import render_dashboard
from pricing_agent.views.improve_aging import render_improve_aging
from pricing_agent.views.promotion import render_promotion_planner
from pricing_agent.views.vehicle_detail import render_vehicle_detail
from pricing_agent.workflows.context import WorkflowContext


class NavigationGroup(str, Enum):
    """Top-level sidebar sections. Skills are never a group — they sit under workflows."""

    DEALER_AI_ASSISTANT = "Dealer AI Assistant"
    DEALER_WORKFLOWS = "Dealer Workflows"


class SkillId(str, Enum):
    """The three reusable analytical capabilities. These match `skills/*/SKILL.md`."""

    SINGLE_VEHICLE_VALUATION = "single-vehicle-valuation"
    INVENTORY_PORTFOLIO_FORECAST = "inventory-portfolio-forecast"
    DEALER_EVENT_PROMOTION_PLANNER = "dealer-event-promotion-planner"


class Availability(str, Enum):
    """How much of an entry is actually built."""

    AVAILABLE = "AVAILABLE"
    """Runs the underlying capability end to end."""

    SHELL_ONLY = "SHELL_ONLY"
    """The screen exists; the capability behind it is not connected yet."""


PROTOTYPE_DISCLAIMER = (
    "Prototype on synthetic data. Forecasts are a configured simulation, not a trained "
    "prediction, and no price can be published from this application."
)

ROUTING_DISCLAIMER = (
    "Natural-language routing is not connected yet — the assistant captures a question "
    "but does not act on it."
)

ORCHESTRATION_DISCLAIMER = (
    "Workflow orchestration is not implemented yet. This page describes the sequence "
    "rather than running it."
)


@dataclass(frozen=True)
class NavigationEntry:
    """Where an entry appears in the sidebar and how it is addressed."""

    title: str
    url_path: str
    icon: str
    group: NavigationGroup
    default: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    """A dealer job, bound to the view that serves it."""

    workflow_id: str
    display_name: str
    description: str
    navigation: NavigationEntry
    render: Callable[..., None]
    context: WorkflowContext | None
    skills: tuple[SkillId, ...] = ()
    availability: Availability = Availability.AVAILABLE
    disclaimer: str = PROTOTYPE_DISCLAIMER

    def bound_render(self) -> Callable[[], None]:
        """The render callable with its workflow context already applied.

        This is what lets one view serve several workflows without being duplicated —
        the binding differs, the view does not.
        """
        return partial(self.render, workflow_context=self.context)

    def as_card(self) -> WorkflowCard:
        """Registry metadata in the shape the assistant home page renders."""
        return WorkflowCard(
            display_name=self.display_name,
            description=self.description,
            icon=self.navigation.icon,
            availability=self.availability.value,
            skills=tuple(skill.value for skill in self.skills),
        )


# --- dealer workflows -----------------------------------------------------------------

DEALER_WORKFLOWS: tuple[WorkflowDefinition, ...] = (
    # Demo narrative order: individual-vehicle pricing first, then portfolio management.
    WorkflowDefinition(
        workflow_id="price-inventory",
        display_name="Price Inventory",
        description=(
            "Value a vehicle against the local market, compare gross against turn, and "
            "see the floor that constrains the price."
        ),
        navigation=NavigationEntry(
            title="Price Inventory",
            url_path="price-inventory",
            icon=":material/sell:",
            group=NavigationGroup.DEALER_WORKFLOWS,
        ),
        render=render_vehicle_detail,
        context=WorkflowContext.PRICE_INVENTORY,
        skills=(SkillId.SINGLE_VEHICLE_VALUATION,),
        availability=Availability.AVAILABLE,
    ),
    WorkflowDefinition(
        workflow_id="acquire-inventory",
        display_name="Acquire Inventory",
        description=(
            "Portfolio capacity, inventory gaps, open slots, and aging and replacement "
            "pressure — what the lot can absorb before buying more."
        ),
        navigation=NavigationEntry(
            title="Acquire Inventory",
            url_path="acquire-inventory",
            icon=":material/inventory:",
            group=NavigationGroup.DEALER_WORKFLOWS,
        ),
        render=render_dashboard,
        context=WorkflowContext.ACQUIRE_INVENTORY,
        skills=(SkillId.INVENTORY_PORTFOLIO_FORECAST,),
        availability=Availability.AVAILABLE,
        # Scoped deliberately: the single-vehicle skill needs a vehicle_id already in
        # dealer inventory, so appraising an external candidate is not something this
        # application can do. Saying so here keeps the claim out of the UI.
        disclaimer=(
            PROTOTYPE_DISCLAIMER
            + " Appraisal of an external acquisition candidate is a future enhancement."
        ),
    ),
    WorkflowDefinition(
        workflow_id="merchandise-inventory",
        display_name="Merchandise Inventory",
        description=(
            "Plan a sale event: which vehicles to discount, by how much, which to protect, "
            "and whether the target is reachable at all."
        ),
        navigation=NavigationEntry(
            title="Merchandise Inventory",
            url_path="merchandise-inventory",
            icon=":material/campaign:",
            group=NavigationGroup.DEALER_WORKFLOWS,
        ),
        render=render_promotion_planner,
        context=WorkflowContext.MERCHANDISE_INVENTORY,
        skills=(SkillId.DEALER_EVENT_PROMOTION_PLANNER,),
        availability=Availability.AVAILABLE,
    ),
    WorkflowDefinition(
        workflow_id="improve-aging-inventory",
        display_name="Improve Aging Inventory",
        description=(
            "Coordinates all three capabilities against aged units — diagnose, select, "
            "price, promote, project, approve."
        ),
        navigation=NavigationEntry(
            title="Improve Aging Inventory",
            url_path="improve-aging-inventory",
            icon=":material/timelapse:",
            group=NavigationGroup.DEALER_WORKFLOWS,
        ),
        render=render_improve_aging,
        context=WorkflowContext.IMPROVE_AGING_INVENTORY,
        # All three: this is an orchestration, not a fourth skill.
        skills=(
            SkillId.INVENTORY_PORTFOLIO_FORECAST,
            SkillId.SINGLE_VEHICLE_VALUATION,
            SkillId.DEALER_EVENT_PROMOTION_PLANNER,
        ),
        availability=Availability.SHELL_ONLY,
        disclaimer=ORCHESTRATION_DISCLAIMER,
    ),
)


def _render_assistant(workflow_context: WorkflowContext | None = None) -> None:
    """Assistant home, with the workflow cards supplied from the registry.

    Passed in rather than imported by the view, so the view has no dependency on this
    module and no import cycle exists.
    """
    render_assistant_home(
        workflow_context,
        workflows=[definition.as_card() for definition in DEALER_WORKFLOWS],
    )


ASSISTANT = WorkflowDefinition(
    workflow_id="ask-the-assistant",
    display_name="Ask the Assistant",
    description=(
        "Describe a decision in your own words. Routing to the right workflow arrives in "
        "the next phase."
    ),
    navigation=NavigationEntry(
        title="Ask the Assistant",
        url_path="ask",
        icon=":material/forum:",
        group=NavigationGroup.DEALER_AI_ASSISTANT,
        default=True,
    ),
    render=_render_assistant,
    # The assistant sits above the workflows rather than inside one.
    context=None,
    skills=(),
    availability=Availability.SHELL_ONLY,
    disclaimer=ROUTING_DISCLAIMER,
)


WORKFLOWS: tuple[WorkflowDefinition, ...] = (ASSISTANT, *DEALER_WORKFLOWS)


def by_id(workflow_id: str) -> WorkflowDefinition:
    for definition in WORKFLOWS:
        if definition.workflow_id == workflow_id:
            return definition
    raise KeyError(f"No workflow registered with id {workflow_id!r}")


def grouped() -> dict[NavigationGroup, list[WorkflowDefinition]]:
    """Registry entries by sidebar group, preserving declaration order."""
    groups: dict[NavigationGroup, list[WorkflowDefinition]] = {}
    for definition in WORKFLOWS:
        groups.setdefault(definition.navigation.group, []).append(definition)
    return groups

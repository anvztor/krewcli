"""Pydantic-graph workflow templates.

Each template defines a Graph whose nodes are task types and edges
are dependencies. The graph structure IS the task decomposition plan.

Node docstrings become task descriptions.
Return type annotations encode dependency edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pydantic_graph import BaseNode, End, Graph, GraphRunContext


# ── Shared state ──

@dataclass
class WorkflowState:
    prompt: str = ""
    recipe_id: str = ""


# ── Feature workflow ──
# Scope → ImplementCore (parallel with WriteTests) → WireIntegration → Review

@dataclass
class FeatureScope(BaseNode[WorkflowState]):
    """Define requirements, identify affected files, plan the approach."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> Union[FeatureImplement, FeatureTests]:
        return FeatureImplement()

@dataclass
class FeatureImplement(BaseNode[WorkflowState]):
    """Build the core functionality with proper error handling."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> FeatureWire:
        return FeatureWire()

@dataclass
class FeatureTests(BaseNode[WorkflowState]):
    """Write unit and integration tests for the new functionality."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> FeatureReview:
        return FeatureReview()

@dataclass
class FeatureWire(BaseNode[WorkflowState]):
    """Connect to routes, UI, or other integration points."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> FeatureReview:
        return FeatureReview()

@dataclass
class FeatureReview(BaseNode[WorkflowState]):
    """Code review, verify all tests pass, check for edge cases."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        return End("done")


def feature_graph() -> Graph:
    return Graph(nodes=[FeatureScope, FeatureImplement, FeatureTests, FeatureWire, FeatureReview])


# ── Bugfix workflow ──
# Diagnose → WriteFailing → ImplementFix → VerifyFix

@dataclass
class BugDiagnose(BaseNode[WorkflowState]):
    """Reproduce the bug, identify root cause, trace the code path."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> BugWriteTest:
        return BugWriteTest()

@dataclass
class BugWriteTest(BaseNode[WorkflowState]):
    """Create a test that captures the bug behavior."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> BugFix:
        return BugFix()

@dataclass
class BugFix(BaseNode[WorkflowState]):
    """Fix the root cause with minimal changes."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> BugVerify:
        return BugVerify()

@dataclass
class BugVerify(BaseNode[WorkflowState]):
    """Run the full test suite, verify the fix resolves the issue."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        return End("done")


def bugfix_graph() -> Graph:
    return Graph(nodes=[BugDiagnose, BugWriteTest, BugFix, BugVerify])


# ── Refactor workflow ──
# Analyze → Plan → Implement → UpdateTests → Document

@dataclass
class RefactorAnalyze(BaseNode[WorkflowState]):
    """Map existing code, identify coupling points and migration risks."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> RefactorPlan:
        return RefactorPlan()

@dataclass
class RefactorPlan(BaseNode[WorkflowState]):
    """Design target architecture, define migration steps."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> RefactorImplement:
        return RefactorImplement()

@dataclass
class RefactorImplement(BaseNode[WorkflowState]):
    """Execute the planned changes incrementally, keeping tests green."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> Union[RefactorUpdateTests, RefactorDocument]:
        return RefactorUpdateTests()

@dataclass
class RefactorUpdateTests(BaseNode[WorkflowState]):
    """Ensure all tests pass, add missing coverage for refactored code."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> RefactorDocument:
        return RefactorDocument()

@dataclass
class RefactorDocument(BaseNode[WorkflowState]):
    """Final review, update documentation and migration notes."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        return End("done")


def refactor_graph() -> Graph:
    return Graph(nodes=[RefactorAnalyze, RefactorPlan, RefactorImplement, RefactorUpdateTests, RefactorDocument])


# ── Review workflow ──
# Scope → Execute → Report

@dataclass
class ReviewScope(BaseNode[WorkflowState]):
    """Identify what needs to be reviewed or tested."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> ReviewExecute:
        return ReviewExecute()

@dataclass
class ReviewExecute(BaseNode[WorkflowState]):
    """Run the review, audit, or test suite."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> ReviewReport:
        return ReviewReport()

@dataclass
class ReviewReport(BaseNode[WorkflowState]):
    """Summarize results, document issues, suggest improvements."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        return End("done")


def review_graph() -> Graph:
    return Graph(nodes=[ReviewScope, ReviewExecute, ReviewReport])


# ── Default workflow ──
# Scope → Implement → Review

@dataclass
class DefaultScope(BaseNode[WorkflowState]):
    """Understand the request, explore the codebase, plan the approach."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> DefaultImplement:
        return DefaultImplement()

@dataclass
class DefaultImplement(BaseNode[WorkflowState]):
    """Execute the planned changes."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> DefaultReview:
        return DefaultReview()

@dataclass
class DefaultReview(BaseNode[WorkflowState]):
    """Verify the implementation, run tests, prepare the digest."""
    async def run(self, ctx: GraphRunContext[WorkflowState]) -> End[str]:
        return End("done")


def default_graph() -> Graph:
    return Graph(nodes=[DefaultScope, DefaultImplement, DefaultReview])

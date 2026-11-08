---
name: codebase-design
description: Shared vocabulary for designing deep modules. Use when designing or improving a module's interface, finding deepening opportunities, deciding where a seam goes, making code more testable or agent-navigable, or when another skill needs the deep-module vocabulary.
---

# Codebase Design

Design **deep modules**: a lot of behaviour behind a small interface, placed at a clean seam, testable through that interface. Use this language and these principles wherever code is being designed or restructured. The aim is leverage for callers, locality for maintainers, and testability for everyone.

## Glossary

Use these terms exactly: don't substitute "component", "service", "API" or "boundary". Consistent language is the whole point.

**Module**: anything with an interface and an implementation. Deliberately scale-agnostic: a function, class, package or tier-spanning slice. _Avoid_: unit, component, service.

**Interface**: everything a caller must know to use the module correctly: the type signature, but also invariants, ordering constraints, error modes, required configuration and performance characteristics. _Avoid_: API, signature (too narrow: they name only the type-level surface).

**Implementation**: what's inside a module, its body of code. Distinct from **Adapter**: a thing can be a small adapter with a large implementation (a Postgres repository) or a large adapter with a small implementation (an in-memory fake). Say "adapter" when the seam is the topic, "implementation" otherwise.

**Depth**: leverage at the interface: the amount of behaviour a caller (or test) can exercise per unit of interface it has to learn. A module is **deep** when a large amount of behaviour sits behind a small interface, **shallow** when the interface is nearly as complex as the implementation.

**Seam** _(Michael Feathers)_: a place where you can alter behaviour without editing in that place; the _location_ at which a module's interface lives. Where the seam goes is its own design decision, distinct from what goes behind it. _Avoid_: boundary (overloaded with DDD's bounded context).

**Adapter**: a concrete thing that satisfies an interface at a seam. Describes _role_ (which slot it fills), not substance (what's inside).

**Leverage**: what callers get from depth: more capability per unit of interface learned. One implementation pays back across N call sites and M tests.

**Locality**: what maintainers get from depth: change, bugs, knowledge and verification concentrate in one place instead of spreading across callers. Fix once, fixed everywhere.

## Deep vs shallow

**Deep module** = small interface + lots of implementation:

```
┌─────────────────────┐
│   Small Interface   │  ← few methods, simple params
├─────────────────────┤
│                     │
│  Deep Implementation│  ← complex logic hidden
│                     │
└─────────────────────┘
```

**Shallow module** = large interface + little implementation (avoid):

```
┌─────────────────────────────────┐
│       Large Interface           │  ← many methods, complex params
├─────────────────────────────────┤
│  Thin Implementation            │  ← just passes through
└─────────────────────────────────┘
```

When designing an interface, ask: can I reduce the number of methods? Simplify the parameters? Hide more complexity inside?

## Principles

- **Depth is a property of the interface, not the implementation.** A deep module can be built from small, mockable, swappable parts; they just aren't part of the interface. A module can have **internal seams** (private to its implementation, used by its own tests) as well as the **external seam** at its interface.
- **The deletion test.** Imagine deleting the module. If complexity vanishes, it was a pass-through. If complexity reappears across N callers, it was earning its keep.
- **The interface is the test surface.** Callers and tests cross the same seam. If you want to test _past_ the interface, the module is probably the wrong shape.
- **One adapter means a hypothetical seam. Two adapters mean a real one.** Don't introduce a seam unless something actually varies across it.

## Designing for testability

1. **Accept dependencies, don't create them.**

   ```python
   # Testable
   def process_order(order, payment_gateway): ...

   # Hard to test
   def process_order(order):
       gateway = StripeGateway()
   ```

2. **Return results, don't produce side effects.**

   ```python
   # Testable
   def calculate_discount(cart) -> Discount: ...

   # Hard to test
   def apply_discount(cart) -> None:
       cart.total -= discount
   ```

3. **Small surface area.** Fewer methods mean fewer tests; fewer parameters mean simpler setup.

## Relationships

- A **Module** has exactly one **Interface** (the surface it presents to callers and tests).
- **Depth** is a property of a **Module**, measured against its **Interface**.
- A **Seam** is where a **Module**'s **Interface** lives.
- An **Adapter** sits at a **Seam** and satisfies the **Interface**.
- **Depth** produces **Leverage** for callers and **Locality** for maintainers.

## Rejected framings

- **Depth as the ratio of implementation lines to interface lines** (Ousterhout): rewards padding the implementation. Depth here is leverage.
- **"Interface" as a language's `interface` keyword or a class's public methods**: too narrow; the interface is every fact a caller must know.
- **"Boundary"**: overloaded with DDD's bounded context. Say **seam** or **interface**.

## Going deeper

These files are in this skill's directory; read them with `read_file` when the branch applies.

- **Deepening a cluster given its dependencies**: [DEEPENING.md](DEEPENING.md): dependency categories, seam discipline, replace-don't-layer testing.
- **Exploring alternative interfaces**: [DESIGN-IT-TWICE.md](DESIGN-IT-TWICE.md): several subagents design the interface radically differently, in parallel, then compare on depth, locality and seam placement.

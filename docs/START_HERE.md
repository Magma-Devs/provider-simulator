# Provider Simulator - Complete Learning Guide (Index)

## Table of Contents
1. [Welcome! Start Here](#welcome-start-here-)
2. [Quick Start (Public Repo)](#quick-start-public-repo)
3. [Documentation Files](#-documentation-files)
   - [1. ARCHITECTURE_GUIDE.md](#1-architecture_guidemd---the-big-picture)
   - [2. CLASS_REFERENCE.md](#2-class_referencemd---deep-dive-into-code)
   - [3. DATA_FLOWS.md](#3-data_flowsmd---how-requests-travel)
4. [Recommended Learning Path](#-recommended-learning-path)
   - [Path 1: I Want to Understand the System](#path-1-i-want-to-understand-the-system-complete-overview)
   - [Path 2: I Want to Read the Code](#path-2-i-want-to-read-the-code-deep-technical)
   - [Path 3: I Want to Deploy It](#path-3-i-want-to-deploy-it-operational)
5. [Quick Navigation](#️-quick-navigation)
6. [Key Concepts Explained Everywhere](#-key-concepts-explained-everywhere)
7. [Deep Dives by Topic](#-deep-dives-by-topic)
8. [Reading Strategies](#-reading-strategies)
9. [Learning Exercises](#-learning-exercises)
10. [Frequently Asked Questions](#-frequently-asked-questions)
11. [Documentation Structure](#-documentation-structure)
12. [Learning Outcomes](#-learning-outcomes)
13. [Next Steps](#-next-steps)
14. [Tips for Effective Learning](#-tips-for-effective-learning)
15. [When You're Stuck](#-when-youre-stuck)
16. [Documentation Quality](#-documentation-quality)
17. [Your Learning Goal](#-your-learning-goal)

---

## Welcome! Start Here 👋

This project contains **comprehensive documentation** designed to teach you everything about the provider simulator in **friendly manner**.

**No prior knowledge required** - we explain everything from scratch.

Public simulator URLs are derived from one simulator-owned setting: `BASE_DOMAIN` in `config/base-domain.env`.

## Quick Start (Public Repo)

Use HTTPS clone by default (no deploy key required for read-only pull):

```bash
git clone https://github.com/Magma-Devs/provider_simulator.git ~/provider-simulator
cd ~/provider-simulator
```

For server bootstrap and production deploy flow, start with `docs/new_server_setup.md`.

---

## 📚 Documentation Files

### 1. **ARCHITECTURE_GUIDE.md** - The Big Picture
**Start here if you want to understand the overall system.**

📍 **Topics covered:**
- What this project does and why it matters
- The three-layer architecture
- File structure and organization
- Module breakdown (server.py, Dockerfile, K8s configs)
- Class relationships
- Data flows (high-level)
- Deployment process

📖 **Best for:**
- Getting the "big picture" understanding
- Understanding how pieces fit together
- Learning what each file does
- Understanding deployment

⏱️ **Reading time:** 30-45 minutes

---

### 2. **CLASS_REFERENCE.md** - Deep Dive into Code
**Start here if you want to understand each class in detail.**

📍 **Topics covered:**
- `ProviderState` class - holds provider state
  - Instance variables
  - `snapshot()` method
  - `update()` method
  - scenario reset method
  - Thread safety explained
- `JSONRPCHandler` class - serves fake responses
  - `do_POST()` method step-by-step
  - `_reply()` helper
  - Request-response examples
- `ControlHandler` class - configures simulator
  - `do_POST()` method
  - `do_GET()` method
  - Endpoint reference
- `main()` function - orchestration
- Complete code walkthroughs
- Class interaction diagrams

📖 **Best for:**
- Understanding what each class does
- Learning how to read the code
- Understanding method logic
- Seeing detailed examples

⏱️ **Reading time:** 45-60 minutes

---

### 3. **DATA_FLOWS.md** - How Requests Travel
**Start here if you want to understand the request lifecycle.**

📍 **Topics covered:**
- Request types (Control vs Router)
- Data flow diagrams for each scenario
- Complete request cycles
- State transitions
- Thread interaction
- Common test scenarios
- Request-response examples

📖 **Best for:**
- Understanding how requests flow through the system
- Seeing what happens at each step
- Understanding state changes
- Learning common test patterns

⏱️ **Reading time:** 45-60 minutes

---

## 🎯 Recommended Learning Path

### Path 1: "I Want to Understand the System" (Complete Overview)
1. **Start:** ARCHITECTURE_GUIDE.md
   - Read: "What This Project Does" section
   - Read: "The Big Picture" section
   - Read: "Architecture Overview" section
2. **Continue:** CLASS_REFERENCE.md
   - Read: "ProviderState Class" section
   - Read: "JSONRPCHandler Class" (first half)
3. **Finish:** DATA_FLOWS.md
   - Read: "Overview" section
   - Read: "Complete Request Cycles" section

📚 **Total time:** ~90 minutes  
✅ **Result:** Solid understanding of architecture and code

---

### Path 2: "I Want to Read the Code" (Deep Technical)
1. **Start:** CLASS_REFERENCE.md
   - Read all sections carefully
   - Pay attention to step-by-step walkthroughs
2. **Reference:** DATA_FLOWS.md
   - Look up specific request flows you're curious about
3. **Context:** ARCHITECTURE_GUIDE.md
   - Refer back for context on Kubernetes and deployment

📚 **Total time:** ~2-3 hours  
✅ **Result:** Deep understanding of every class and method

---

### Path 3: "I Want to Deploy It" (Operational)
1. **Quick overview:** ARCHITECTURE_GUIDE.md
   - Read: "The Big Picture" section
   - Read: "Deployment Process" section
2. **File reference:** ARCHITECTURE_GUIDE.md
   - Read: "Module Breakdown" sections for:
     - `scripts/deploy.sh`
     - `k8s/deployment.yml`
     - `Dockerfile`
3. **Troubleshooting:** DATA_FLOWS.md
   - Read: "Common Scenarios" section

📚 **Total time:** ~45 minutes  
✅ **Result:** Ready to deploy and troubleshoot

---

## 🗺️ Quick Navigation

### If I want to know about...

| Topic | Location | Section |
|-------|----------|---------|
| What does the simulator do? | ARCHITECTURE_GUIDE | "What This Project Does" |
| How do the 3 layers work? | ARCHITECTURE_GUIDE | "Architecture Overview" |
| What is ProviderState? | CLASS_REFERENCE | "ProviderState Class" |
| How does JSONRPCHandler work? | CLASS_REFERENCE | "JSONRPCHandler Class" |
| What is the control API? | CLASS_REFERENCE | "ControlHandler Class" |
| How do threads work together? | DATA_FLOWS | "Thread Interaction" |
| What happens in a typical test? | DATA_FLOWS | "Complete Request Cycles" |
| How does failover work? | DATA_FLOWS | "Scenario 1: Testing Failover" |
| What does the Dockerfile do? | ARCHITECTURE_GUIDE | "Dockerfile - Container Configuration" |
| How do I deploy this? | ARCHITECTURE_GUIDE | "Deployment Process" |
| What are the Kubernetes manifests? | ARCHITECTURE_GUIDE | "k8s/deployment.yml", "k8s/service.yml", "k8s/httproute-control.yml" |
| How do HTTP requests flow? | DATA_FLOWS | "Data Flow Diagrams" |
| What are the provider modes? | CLASS_REFERENCE | "JSONRPCHandler - do_POST()" |
| How does latency injection work? | DATA_FLOWS | "Flow 4: Latency Injection Path" |
| How does error probability work? | DATA_FLOWS | "Flow 5: Error Probability Path" |

---

## 💡 Key Concepts Explained Everywhere

### Thread Safety
- **ARCHITECTURE_GUIDE:** Brief mention
- **CLASS_REFERENCE:** "ProviderState - Thread Safety Explained"
- **DATA_FLOWS:** "Thread Interaction" section

### Request Lifecycle
- **ARCHITECTURE_GUIDE:** "Data Flows" section
- **CLASS_REFERENCE:** "Complete Code Walkthrough"
- **DATA_FLOWS:** "Complete Request Cycles" section

### Class Relationships
- **ARCHITECTURE_GUIDE:** "Class Relationships" section
- **CLASS_REFERENCE:** "Class Interaction Diagram"
- **DATA_FLOWS:** Throughout

### Provider Modes
- **CLASS_REFERENCE:** "JSONRPCHandler - do_POST()" (each mode explained)
- **DATA_FLOWS:** "Data Flow Diagrams" (visual examples of each)
- **ARCHITECTURE_GUIDE:** "Module Breakdown - server.py"

---

## 🔍 Deep Dives by Topic

### Understanding ProviderState

**Basic overview:**
- ARCHITECTURE_GUIDE → "Module Breakdown" → "Part 1: ProviderState"

**Deep technical:**
- CLASS_REFERENCE → "ProviderState Class" (entire section)

**In action:**
- DATA_FLOWS → "Flow 1-6: Various scenarios"

**Thread safety:**
- CLASS_REFERENCE → "ProviderState - Thread Safety Explained"

---

### Understanding JSONRPCHandler

**Basic overview:**
- ARCHITECTURE_GUIDE → "Module Breakdown" → "Part 2: JSONRPCHandler"

**Step-by-step logic:**
- CLASS_REFERENCE → "JSONRPCHandler - do_POST()" (detailed walkthrough)

**In action:**
- DATA_FLOWS → "Complete Request Cycles"

**Provider modes:**
- CLASS_REFERENCE → "JSONRPCHandler - do_POST()" (each mode)
- DATA_FLOWS → "Flow 1-5: Different modes"

---

### Understanding ControlHandler

**Basic overview:**
- ARCHITECTURE_GUIDE → "Module Breakdown" → "Part 3: ControlHandler"

**Endpoints reference:**
- CLASS_REFERENCE → "ControlHandler - Endpoints"

**Method details:**
- CLASS_REFERENCE → "ControlHandler - do_POST()" and "do_GET()"

**In action:**
- DATA_FLOWS → "Flow 6: Control API Update Path"

---

### Understanding Kubernetes Deployment

**Overview:**
- ARCHITECTURE_GUIDE → "Architecture Overview" → "File Structure"

**File-by-file:**
- ARCHITECTURE_GUIDE → "Module Breakdown" → "3. k8s/deployment.yml", etc.

**Deployment process:**
- ARCHITECTURE_GUIDE → "Deployment Process"

---

## 📖 Reading Strategies

### Strategy 1: Linear Reading
Read one document completely from start to finish.

**Best for:** Learning a new topic thoroughly  
**Time:** 30-60 minutes per document

---

### Strategy 2: Topic-Based Jumping
Use the Quick Navigation table to jump to sections about your specific interest.

**Best for:** Finding specific information quickly  
**Time:** 5-15 minutes per topic

---

### Strategy 3: Cross-Reference Deep Dive
Pick a concept, read it in ARCHITECTURE_GUIDE, then dive deeper in CLASS_REFERENCE, then see it in action in DATA_FLOWS.

**Best for:** Deep understanding of concepts  
**Time:** 30-45 minutes per concept

---

## 🧪 Learning Exercises

After reading, try these exercises to reinforce understanding:

### Exercise 1: Trace a Request
Pick a specific request (e.g., "test sets provider 1 to down"):
1. Find it in DATA_FLOWS
2. Read the diagram
3. Look up each class mentioned in CLASS_REFERENCE
4. Trace the entire flow

---

### Exercise 2: Find the Code
Read a scenario in DATA_FLOWS, then find the actual code in CLASS_REFERENCE:
1. Read "Flow 3: Rate Limit Path" in DATA_FLOWS
2. Find "if snap['mode'] == 'rate_limit':" in CLASS_REFERENCE
3. Understand what that code does

---

### Exercise 3: Predict Output
Given a scenario, predict what HTTP status code will be returned:
1. "Provider state: mode='success', latency_ms=100"
2. Trace through JSONRPCHandler.do_POST() logic
3. Predict: HTTP 200
4. Verify in DATA_FLOWS

---

### Exercise 4: Understand Thread Interaction
Read "Thread Interaction" in DATA_FLOWS, then:
1. Explain why the lock is needed
2. What happens without the lock?
3. When does each thread acquire the lock?

---

## ❓ Frequently Asked Questions

### "Where do I start?"
👉 Start with **ARCHITECTURE_GUIDE.md** section "The Big Picture"

### "I don't understand ProviderState"
👉 Read **CLASS_REFERENCE.md** section "ProviderState Class" (step-by-step)

### "How do requests flow through the system?"
👉 Read **DATA_FLOWS.md** section "Complete Request Cycles"

### "I want to understand everything"
👉 Follow **Path 1: Complete Overview** above

### "I need to deploy this"
👉 Follow **Path 3: Operational** above

### "I want to read the actual code"
👉 Follow **Path 2: Deep Technical** above

### "What does a specific method do?"
👉 Use **Quick Navigation** table to find the section

### "How do threads interact?"
👉 Read **DATA_FLOWS.md** section "Thread Interaction"

---

## 📊 Documentation Structure

```
ARCHITECTURE_GUIDE.md
├─ High-level overview
├─ What the system does
├─ File organization
├─ Each module explained (what it does)
├─ How pieces fit together
└─ Deployment overview

CLASS_REFERENCE.md
├─ Each class in detail
├─ Instance variables explained
├─ Methods step-by-step
├─ Code walkthroughs
└─ Class interactions

DATA_FLOWS.md
├─ Request types
├─ Visual diagrams
├─ Complete request cycles
├─ State transitions
├─ Thread interactions
└─ Real-world scenarios
```

---

## 🎓 Learning Outcomes

After reading this documentation, you will understand:

✅ **What the simulator does** - It fakes blockchain providers for testing  
✅ **Why it exists** - Makes testing the router easier and faster  
✅ **How it's organized** - Three layers: Testing, Infrastructure, Router  
✅ **Each class and method** - What it does and how it works  
✅ **How requests flow** - From test → control API → router requests → responses  
✅ **Thread safety** - Why locks matter and how they work  
✅ **Provider modes** - success, error, rate_limit, down, latency, error_probability  
✅ **Deployment** - Docker, Kubernetes, HTTPRoute, everything  
✅ **How to extend it** - Adding new features based on understanding  

---

## 🚀 Next Steps

1. **Read the documentation** - Start with ARCHITECTURE_GUIDE.md
2. **Understand the code** - Read CLASS_REFERENCE.md and look at server.py
3. **Trace requests** - Follow examples in DATA_FLOWS.md
4. **Deploy it** - Follow Kubernetes deployment instructions
5. **Write tests** - Use what you learned to understand how tests use it

---

## 📝 Tips for Effective Learning

1. **Take notes** - Write down key concepts as you read
2. **Draw diagrams** - Try to sketch the architecture yourself
3. **Trace examples** - Follow step-by-step walkthroughs carefully
4. **Ask "why"** - Understand the reasoning, not just the code
5. **Cross-reference** - When you see a class mentioned, look it up
6. **Practice prediction** - Before reading what happens, guess
7. **Review often** - Come back to concepts multiple times

---

## 🆘 When You're Stuck

| Situation | What to do |
|-----------|-----------|
| "I don't understand ProviderState" | Read CLASS_REFERENCE "ProviderState Class" |
| "I don't understand requests" | Read DATA_FLOWS "Complete Request Cycles" |
| "I don't understand how it all fits" | Read ARCHITECTURE_GUIDE "Class Relationships" |
| "I want to see the actual code" | Read CLASS_REFERENCE "Complete Code Walkthrough" |
| "I want to see it in action" | Read DATA_FLOWS "Common Scenarios" |
| "I want to understand deployment" | Read ARCHITECTURE_GUIDE "Deployment Process" |

---

## 📚 Documentation Quality

All documentation features:
- ✅ Simple, beginner-friendly language
- ✅ Step-by-step explanations
- ✅ Real code examples
- ✅ ASCII diagrams
- ✅ Multiple perspectives (architecture, code, flow)
- ✅ Cross-references between documents
- ✅ Practical exercises
- ✅ Real-world scenarios

---

## 🎯 Your Learning Goal

After completing this documentation, you should be able to:

1. **Explain to someone else** what the simulator does
2. **Read the code** and understand what each class does
3. **Trace a request** from test through to response
4. **Understand why** thread safety matters
5. **Deploy the system** to Kubernetes
6. **Write tests** that use the simulator
7. **Debug issues** by understanding the flow
8. **Extend the code** with new features

---

**Happy learning! 🚀**

Start with **ARCHITECTURE_GUIDE.md** if you're unsure where to begin.


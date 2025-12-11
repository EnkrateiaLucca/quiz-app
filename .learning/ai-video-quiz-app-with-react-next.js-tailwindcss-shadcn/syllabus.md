# AI Video Quiz App - Learning Syllabus

## 🎯 What You'll Build

By the end of this journey, you'll have built a **working AI-powered quiz app** that:
- Takes any YouTube video URL
- Generates quiz questions from video transcripts using AI
- Shows clickable timestamps that jump to the exact moment in the video
- Looks professional with shadcn/ui components

**Tech Stack:** React, Next.js 14, TailwindCSS, shadcn/ui, YouTube API, Claude/OpenAI API

---

## Prerequisites

### Tools to Install
- [ ] Node.js 18+ (`node --version` to check)
- [ ] npm or pnpm package manager
- [ ] VS Code or Cursor IDE
- [ ] Git (optional but recommended)

### Accounts Needed
- [ ] GitHub account (free)
- [ ] Anthropic API key OR OpenAI API key (for AI quiz generation)
- [ ] Vercel account (free - for deployment later)

### Beginner-Friendly Setup
Don't worry if you're new to React! We'll build step-by-step. Each micro-project teaches ONE concept before combining them.

---

## Learning Objectives

By the end, you will be able to:
1. ✅ Create React components from scratch
2. ✅ Build pages and routing in Next.js
3. ✅ Style with TailwindCSS utility classes
4. ✅ Use shadcn/ui components (buttons, cards, dialogs)
5. ✅ Fetch and display data from APIs
6. ✅ Build a working AI-powered quiz application
7. ✅ Deploy your app to the web

---

## Learning Path

### Phase 1: Foundations (Week 1) - Micro Projects 🔨
*Goal: Ship 6 tiny working demos, each teaching ONE concept*

- [ ] **1.1** Hello Next.js - Create your first Next.js app (15 min)
  - 🔨 Build: A page that says "My Quiz App"
  - ✅ Done when: You see it in browser at localhost:3000

- [ ] **1.2** Your First Component - Learn React components (20 min)
  - 🔨 Build: A `<QuizCard>` component that displays a question
  - ✅ Done when: Card shows question text with 4 answer buttons

- [ ] **1.3** TailwindCSS Basics - Style without CSS files (20 min)
  - 🔨 Build: Style your QuizCard with colors, spacing, hover effects
  - ✅ Done when: Card looks professional (rounded corners, shadows, colors)

- [ ] **1.4** shadcn/ui Components - Use pre-built UI (25 min)
  - 🔨 Build: Replace your buttons with shadcn `<Button>` components
  - ✅ Done when: Using shadcn Button, Card, and Badge components

- [ ] **1.5** React State - Make things interactive (25 min)
  - 🔨 Build: Quiz card that highlights selected answer
  - ✅ Done when: Clicking an answer highlights it, shows correct/incorrect

- [ ] **1.6** Multiple Questions - Loop through data (30 min)
  - 🔨 Build: Display 5 hardcoded quiz questions with navigation
  - ✅ Done when: Can click "Next" to see each question, shows score at end

**🎉 Phase 1 Complete: You've built a working static quiz app!**

---

### Phase 2: Adding Video Integration (Week 2) - Mini Projects 🔨
*Goal: Connect your quiz to real YouTube videos*

- [ ] **2.1** Embed YouTube Videos (30 min)
  - 🔨 Build: Video player component that embeds any YouTube video
  - ✅ Done when: Paste URL → video plays in your app

- [ ] **2.2** Clickable Timestamps (30 min)
  - 🔨 Build: Timestamp buttons that jump video to specific time
  - ✅ Done when: Click "2:34" → video jumps to 2 minutes 34 seconds

- [ ] **2.3** Video + Quiz Layout (30 min)
  - 🔨 Build: Split-screen layout (video left, quiz right)
  - ✅ Done when: Professional 2-column layout with shadcn components

- [ ] **2.4** Fetch YouTube Transcripts (45 min)
  - 🔨 Build: API route that fetches video transcript
  - ✅ Done when: Enter URL → see transcript text with timestamps

**🎉 Phase 2 Complete: Your quiz app plays videos with clickable timestamps!**

---

### Phase 3: AI-Powered Quiz Generation (Week 3) 🔨
*Goal: Generate quiz questions automatically from any video*

- [ ] **3.1** API Routes in Next.js (30 min)
  - 🔨 Build: Backend API route that accepts POST requests
  - ✅ Done when: Can send data to /api/generate and get response

- [ ] **3.2** Connect to AI (Claude/OpenAI) (45 min)
  - 🔨 Build: API route that sends transcript to AI, gets quiz back
  - ✅ Done when: Send transcript → get 5 quiz questions as JSON

- [ ] **3.3** Quiz Generation Flow (45 min)
  - 🔨 Build: Full flow: URL → transcript → AI → quiz questions
  - ✅ Done when: Paste YouTube URL → loading... → quiz appears!

- [ ] **3.4** Timestamps in Questions (30 min)
  - 🔨 Build: AI generates timestamps with each question
  - ✅ Done when: Each question has clickable timestamp that jumps to relevant video section

**🎉 Phase 3 Complete: Your AI generates quizzes from any YouTube video!**

---

### Phase 4: Polish & Ship (Week 4) 🚀
*Goal: Make it production-ready and deploy*

- [ ] **4.1** Loading States & Error Handling (30 min)
  - 🔨 Build: Skeleton loaders, error messages, retry buttons
  - ✅ Done when: App handles slow/failed requests gracefully

- [ ] **4.2** Results & Score Screen (30 min)
  - 🔨 Build: Final score screen with performance breakdown
  - ✅ Done when: Shows score, wrong answers with correct timestamps

- [ ] **4.3** Responsive Design (30 min)
  - 🔨 Build: Works great on mobile and desktop
  - ✅ Done when: Looks good at all screen sizes

- [ ] **4.4** Deploy to Vercel (20 min)
  - 🔨 Build: Live app on the internet!
  - ✅ Done when: Anyone can access your app via URL

**🎉 Phase 4 Complete: You shipped a production app! 🚀**

---

## Common Gotchas & Fixes

### "Module not found" errors
```bash
# Fix: Install missing package
npm install [package-name]
```

### "Hydration mismatch" errors
```
# Fix: This happens when server/client render differently
# Usually: Don't use window/document in component body
# Use useEffect for browser-only code
```

### YouTube API quota exceeded
```
# Fix: Use youtube-transcript library instead of official API
# npm install youtube-transcript
```

### AI response not valid JSON
```
# Fix: Add "respond ONLY with valid JSON" to your prompt
# Parse with try/catch, show error message
```

### TailwindCSS not working
```bash
# Fix: Make sure tailwind.config.js content paths are correct
content: ["./app/**/*.{js,ts,jsx,tsx}"]
```

---

## Teaching Milestones

After each phase, you'll explain what you built:

- **After Phase 1:** "Explain how React components work and why we use state"
- **After Phase 2:** "Teach someone how YouTube embedding and timestamps work"
- **After Phase 3:** "Explain the API → AI → JSON → UI data flow"
- **After Phase 4:** "Demo your app and explain the architecture"

---

## Project Iteration Path

### v1 - Make It Work (This syllabus)
- Hardcoded video URLs
- Basic styling
- Single quiz per session
- Local development only

### v2 - Make It Better (Future)
- [ ] Save quiz history with localStorage
- [ ] Multiple quiz modes (timed, practice, review)
- [ ] Share quizzes with friends
- [ ] Dark mode toggle

### v3 - Make It Right (Advanced)
- [ ] User accounts and saved progress
- [ ] Custom video upload (not just YouTube)
- [ ] Quiz analytics and spaced repetition
- [ ] Database integration

---

## Resources

### Official Docs (Reference when stuck)
- [Next.js Docs](https://nextjs.org/docs) - App Router section
- [TailwindCSS Docs](https://tailwindcss.com/docs)
- [shadcn/ui Docs](https://ui.shadcn.com/docs)
- [React Docs](https://react.dev/learn)

### YouTube Tutorials (Watch if you get stuck)
- "Next.js 14 Crash Course" - Traversy Media
- "TailwindCSS in 100 Seconds" - Fireship
- "shadcn/ui Tutorial" - Web Dev Simplified

### AI API Docs
- [Anthropic Claude API](https://docs.anthropic.com/)
- [OpenAI API](https://platform.openai.com/docs)

---

## Success Criteria

You've succeeded when you can check ALL of these:

- [ ] 🚀 Shipped a working quiz app to the internet
- [ ] 🎥 App generates quizzes from any YouTube video
- [ ] ⏱️ Timestamps click and jump to video moments
- [ ] 🤖 AI generates relevant questions automatically
- [ ] 💅 App looks professional with shadcn/ui
- [ ] 🗣️ Can explain how it works to someone else
- [ ] 📁 Code is organized and you understand every file

---

## Quick Start - First 15 Minutes! 🏃

Ready to build? Let's ship something TODAY:

```bash
# 1. Create new Next.js app
npx create-next-app@latest quiz-app --typescript --tailwind --eslint --app

# 2. Enter project
cd quiz-app

# 3. Install shadcn/ui
npx shadcn@latest init

# 4. Add your first component
npx shadcn@latest add button card

# 5. Start development server
npm run dev
```

Now open `localhost:3000` - you're ready to build! 🎉

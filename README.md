# Quiz App

A modern, user-friendly quiz application that supports multiple-choice and open-ended questions. The app allows users to import quiz questions from JSON files, take quizzes, and export results in various formats including CSV and Anki-compatible formats.

## Features

- Multiple choice and open-ended question support
- JSON-based quiz file import
- Real-time feedback on answers (visual)
- Score tracking
- Export results to CSV
- Export to Anki format for flashcard creation
- Modern, responsive UI

## Getting Started

### Prerequisites

- A modern web browser (Chrome, Firefox, Safari, Edge)
- Basic knowledge of JSON formatting

### Installation

1. Clone the repository:
```bash
git clone https://github.com/EnkrateiaLucca/quiz-app.git
```

2. Navigate to the project directory:
```bash
cd quiz-app
```

3. Open `quiz-app.html` in your web browser

### Usage

1. Prepare your quiz file in JSON format (see example below)
2. Click "Choose Quiz File" to select your JSON file
3. Click "Start Quiz" to begin
4. Answer the questions
5. View your results and export them if desired

### Quiz File Format

Your quiz file should be a JSON file with the following structure:

```json
[
  {
    "question": "What is the capital of France?",
    "type": "multiple-choice",
    "options": ["London", "Paris", "Berlin", "Madrid"],
    "correctAnswer": 1
  },
  {
    "question": "Explain the concept of photosynthesis.",
    "type": "open-ended",
    "acceptedAnswers": ["The process by which plants convert sunlight into energy", "Plants use sunlight to make food"]
  }
]
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Inspired by the need for simple, effective quiz tools 
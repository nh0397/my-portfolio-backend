from flask import Flask, request, jsonify, session
from flask_cors import CORS
from flask_session import Session
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import google.generativeai as genai
import numpy as np
from urllib.parse import quote_plus
import json
import re

# Load environment variables
load_dotenv()

# Determine the base URL based on the environment
FLASK_ENV = os.getenv('FLASK_ENV')
if FLASK_ENV == 'production':
    BASE_URL = os.getenv('PRODUCTION_URL')
else:
    BASE_URL = os.getenv('DEVELOPMENT_URL')

# Encode the MongoDB username and password
user_name = quote_plus(os.getenv('MONGO_USERNAME'))
password = quote_plus(os.getenv('MONGO_PASSWORD'))

# Construct the MongoDB URI
MONGO_URI = f"mongodb+srv://{user_name}:{password}@cluster0.5hufumz.mongodb.net/{os.getenv('MONGO_DB_NAME')}?retryWrites=true&w=majority&appName=Cluster0"

app = Flask(__name__)

development_url = os.getenv('DEVELOPMENT_URL')
production_url = os.getenv('PRODUCTION_URL')

# Configure CORS with dynamic origins based on environment
CORS(app, resources={r"/*": {"origins": [development_url, production_url]}})

# Session configuration
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

# MongoDB connection
client = MongoClient(MONGO_URI)
db = client[os.getenv('MONGO_DB_NAME')]

# Configure Gemini API
GEMINI_API_KEY = os.getenv('GOOGLE_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)

class GoogleEmbeddings:
    def __init__(self, model_name: str = "models/embedding-001") -> None:
        self.model_name = model_name

    def generate_embeddings(self, inp: str) -> np.ndarray:
        if not GEMINI_API_KEY:
            print("Please set correct Google API key")
            return []

        genai.configure(api_key=GEMINI_API_KEY)
        result = genai.embed_content(model=self.model_name,
                                     content=inp,
                                     task_type="retrieval_document",)

        try:
            embds = np.array(result.get("embedding", []))
        except:
            print("Embeddings not found")
            return []

        return list(list(embds.reshape(1, -1))[0])

# Function to format the text based on symbols (*, **)
def format_text(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)  # Bold
    text = re.sub(r'\*(.*?)\n', r'<ul><li>\1</li></ul>', text)  # Bullet Points
    return text

# Function to manage conversation context within the session
def get_conversation_context(session_id, max_tokens=1000):
    if 'messages' in session:
        context_text = session['messages']
        token_count = len(context_text.split())  # Simple token estimation
        if token_count > max_tokens:
            # Truncate context to fit within token limits
            context_text = " ".join(context_text.split()[-max_tokens:])
        return context_text
    return ""

# Function to save the conversation context within the session
def save_conversation_context(session_id, message, response):
    if 'messages' in session:
        session['messages'] += f"\nUser: {message}\nBot: {response}"
    else:
        session['messages'] = f"User: {message}\nBot: {response}"

# Function to find similar documents using vector search
def find_similar_documents(
    collection,
    inp_document_embedding: list,
    index_name: str,
    col_name: str,
    no_of_docs: int = 3,
    query: dict = {},
) -> list:
    documents = collection.aggregate(
        [
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": col_name,
                    "queryVector": inp_document_embedding,
                    "numCandidates": 49,
                    "limit": no_of_docs,
                }
            },
            {"$match": query},
            {
                "$project": {
                    "resume_data": 1,
                    "github_data": 1,
                    "linkedin_data": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
    )
    return list(documents)[:no_of_docs]

# Route for chatbot queries
@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    message = data.get('message')
    session_id = request.cookies.get('session')  # Get session ID from cookies

    # Get conversation context
    context = get_conversation_context(session_id)

    # Initial prompt to let the LLM decide the type of message
    initial_prompt = f"You are conversation classifier. If the message contains generic questions around your abilites, return 'generic' and if it talks about Naisarg or anything about him (indirectly using pronouns) return 'context-specific'. Now here's the user message that you must classify ': {message}"
    classification_response = genai.GenerativeModel('gemini-1.5-flash').generate_content(initial_prompt)
    classification = classification_response.text.strip().lower()
    print('The API call is ', classification)
    if 'context-specific' in classification:
        # Convert message to an embedding vector
        google_embeddings = GoogleEmbeddings()
        message_embedding = google_embeddings.generate_embeddings(message)

        # Find similar documents in MongoDB
        similar_docs = find_similar_documents(
            collection=db[os.getenv('MONGO_CL_NAME')],
            inp_document_embedding=message_embedding,
            index_name=os.getenv('MONGO_INDEX_NAME'),
            col_name=os.getenv('MONGO_EMBEDDING_FIELD_NAME'),
            no_of_docs=3
        )

        # Extract and parse the text content from similar documents
        similar_texts = []
        for doc in similar_docs:
            for key in ["resume_data", "github_data", "linkedin_data"]:
                if key in doc:
                    try:
                        parsed_data = json.loads(doc[key])
                        similar_texts.append(json.dumps(parsed_data, indent=2))
                    except json.JSONDecodeError as e:
                        print(f"Error parsing JSON data: {e}")

        combined_texts = "\n".join(similar_texts)

        # Prepare the prompt using the context-specific information
        prompt = f"{context}\nYou are Naisarg's AI assistant and I need you to understand the following information: \n{combined_texts}\n\nNow the user wants a crisp answer for the following question: {message}. So please answer in third person."

    else:
        # For casual or non-specific conversations, prompt without vector search
        print('casual')
        prompt = f"You are Naisarg's AI Buddy and this a generic question asked by the user to you. The message is {message}. Tell them how much you would love to answer these but for now you are configured to answer questions related to Naisarg and his professional life."

    # Generate a response using Gemini LLM
    response = genai.GenerativeModel('gemini-1.5-flash').generate_content(prompt)
    response_text = format_text(response.text)

    # Save conversation context
    save_conversation_context(session_id, message, response_text)

    return jsonify({'response': response_text})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

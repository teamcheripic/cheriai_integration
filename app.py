from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return jsonify({"message": "Flask API is running!"})

@app.route('/hello', methods=['GET'])
def hello():
    return jsonify({"message": "Hello from Flask!"})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")

    # Example logic
    reply = f"You said: {user_message}"

    return jsonify({
        "reply": reply,
        "status": "success"
    })

if __name__ == '__main__':
    # host=0.0.0.0 makes it visible in LAN (for mobile testing)
    app.run(host='0.0.0.0', port=5000, debug=True)

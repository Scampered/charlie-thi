// All requests go to "api/..." on the same address the page was opened from
// (http://localhost:5002, http://192.168.x.x:5002, the university server ...).
// nginx forwards everything under /api/ to the Flask container, so no IP address
// has to be written here anymore.
const URL = 'api/';

function startTraining() {
    fetch(URL + 'train')
    .then(response => {
        if (response.ok) {
            return response.text();
        }
    })
    .then(function(result) {
        console.log(result);
        alert('Training result: ' + result);
    })
    .catch(function(error) {
        console.error(error);
        alert('Error: ' + error);
    });
}

function sendMessage() {
    const userMessage = document.getElementById('user-message').value;

    if (userMessage.trim() === '') {
        return;
    }
    appendMessage('You', 'userstyle', userMessage);
    callCharServerAndStoreResponse(userMessage);
}

function appendMessage(sender, styleClass, message) {
    const chatLog = document.getElementById('chat-log');
    const messageElement = document.createElement('div');

    messageElement.classList.add(styleClass);
    // textContent (not innerHTML), so a message containing < or > is shown as text
    messageElement.textContent = sender + ': ' + message;
    chatLog.appendChild(messageElement);
}

function callCharServerAndStoreResponse(userMessage) {
    // encodeURIComponent: spaces, ? and & in the question would otherwise break the URL
    fetch(URL + 'chat?question=' + encodeURIComponent(userMessage))
    .then(response => {
        if (response.ok) {
            return response.text();
        }
    })
    .then(function(data) {
        console.log(data);

        appendMessage('Charlie',  'botstyle', data);
        const log = document.getElementById("chat-log");
        log.scrollTop = log.scrollHeight;
        document.getElementById('user-message').value = '';
    })
    .catch(function(error) {
        console.error(error);
        alert('Error: server not available - is Docker running? Try: docker compose up -d');
    });
}

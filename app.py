import os
from flask import Flask, render_template

# Flask constructor takes the name of current module (__name__) as argument.
app = Flask(__name__)

# The route() function of the Flask class is a decorator, which tells the application which URL should call the associated function.
@app.route('/')
# ‘/’ URL is bound with home() function.
def home():
    return render_template('home.html')

# main driver function
if __name__ == '__main__':

    # run() method of Flask class runs the application on the local development server.
    app.run(debug=True)
    port = int(os.environ.get("PORT", 5000))
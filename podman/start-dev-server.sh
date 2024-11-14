while ! nc -z database 5432; do
    echo "Waiting for database..."
    sleep 1
done

echo "############################"
echo "##### Database reached #####"
echo "############################"

python3 -m venv ./venv
source venv/bin/activate

python3 -m pip install --upgrade pip
python3 -m pip install wheel
python3 -m pip install -r requirements.txt 

flask init-db

flask run -h 0.0.0.0 -p 5000

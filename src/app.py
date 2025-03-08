import os
import json
import base64
import sys
import logging
import traceback
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import google.generativeai as genai
import tempfile
import pandas as pd
from PIL import Image
import pdf2image
import csv
import io
import re
from datetime import datetime

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Gemini APIの設定
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    logger.info("Gemini API configured successfully")
else:
    logger.warning("警告: GOOGLE_API_KEYが設定されていません。")

# Flaskアプリの設定
app = Flask(__name__, static_folder='.')

# アップロードフォルダの設定を絶対パスに変更
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(tempfile.gettempdir(), 'payment_statement_uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB制限

# アップロードフォルダが存在しない場合は作成
try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    logger.info(f"Upload folder created at: {app.config['UPLOAD_FOLDER']}")
except Exception as e:
    logger.error(f"Error creating upload folder: {e}")

# 許可されるファイル拡張子
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

# プログレス情報を保持するグローバル変数
progress_data = {
    'current_file': 0,
    'total_files': 0,
    'current_page': 0,
    'total_pages': 0,
    'filename': ''
}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_pdf_to_images(pdf_path):
    """PDFをPIL Imageのリストに変換"""
    return pdf2image.convert_from_path(pdf_path)

def process_image(image):
    """画像をGemini APIで処理"""
    if not GOOGLE_API_KEY:
        return {"error": "GOOGLE_API_KEYが設定されていません", "records": []}
        
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # プロンプトの設定
    prompt = """
    これは日本の支払調書です。以下の情報を抽出してください：
    1. 区分
    2. 細目
    3. 支払金額
    4. 源泉徴収税額
    5. 支払者の所在地
    6. 支払者の名称
    7. 支払者の電話番号
    
    支払者の情報は通常、支払調書の下側に配置されています。
    
    結果はJSON形式で返してください。複数の支払調書データがある場合は配列形式で返してください。
    
    例：
    {
        "records": [
            {
                "category": "報酬・料金・契約金及び賞金の支払調書",
                "detail": "原稿料",
                "payment_amount": 100000,
                "withholding_tax": 10210,
                "payer_address": "東京都千代田区丸の内1-1-1",
                "payer_name": "株式会社サンプル",
                "payer_tel": "03-1234-5678"
            }
        ]
    }
    """
    
    # 画像をバイト列に変換
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='PNG')
    img_byte_arr = img_byte_arr.getvalue()
    img_base64 = base64.b64encode(img_byte_arr).decode('utf-8')
    
    try:
        # Gemini APIにリクエスト
        response = model.generate_content([
            prompt,
            {"mime_type": "image/png", "data": img_base64}
        ])
        
        # レスポンスからJSONを抽出
        try:
            json_str = response.text
            # JSONの部分を抽出（```json と ``` で囲まれた部分）
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', json_str)
            if json_match:
                json_str = json_match.group(1)
            
            # JSONをパース
            data = json.loads(json_str)
            
            return data
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            print(f"Attempted to parse: {json_str}")
            return {"error": f"JSONの解析に失敗しました: {e}", "records": []}
    except Exception as e:
        print(f"Error parsing Gemini response: {e}")
        print(f"Raw response: {response.text if 'response' in locals() else 'No response'}")
        return {"error": str(e), "records": []}

def aggregate_records(records):
    """同じ支払者と区分のレコードを集計"""
    aggregated = {}
    
    for record in records:
        key = (record['payer_name'], record['category'], record['detail'])
        if key not in aggregated:
            aggregated[key] = {
                'category': record['category'],
                'detail': record['detail'],
                'payment_amount': 0,
                'withholding_tax': 0,
                'payer_address': record['payer_address'],
                'payer_name': record['payer_name'],
                'payer_tel': record['payer_tel'],
                'count': 0
            }
        
        # 金額を集計
        aggregated[key]['payment_amount'] += record.get('payment_amount', 0) or 0
        aggregated[key]['withholding_tax'] += record.get('withholding_tax', 0) or 0
        aggregated[key]['count'] += 1
    
    return list(aggregated.values())

# ルートパスへのアクセスを処理するルート
@app.route('/')
def serve_root():
    try:
        return send_from_directory('.', 'index.html')
    except Exception as e:
        logger.error(f"Error serving root: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"error": "Could not serve index.html"}), 500

# プログレス情報を提供するエンドポイント
@app.route('/progress')
def get_progress():
    """ファイル処理の進捗状況をJSON形式で返すエンドポイント"""
    global progress_data
    return jsonify(progress_data)

# ヘルスチェック用のエンドポイント
@app.route('/health')
def health_check():
    # 拡張されたヘルスチェックエンドポイント
    try:
        # Heroku環境を検出
        is_heroku = 'DYNO' in os.environ
        environment = "Heroku" if is_heroku else "Local/Cloud Run"
        
        # ポート情報を取得
        port = int(os.environ.get('PORT', 8080))
        
        # システム情報を取得
        system_info = {
            "status": "healthy",
            "environment": environment,
            "timestamp": datetime.now().isoformat(),
            "python_version": sys.version,
            "api_key_configured": bool(GOOGLE_API_KEY),
            "upload_folder": app.config['UPLOAD_FOLDER'],
            "upload_folder_exists": os.path.exists(app.config['UPLOAD_FOLDER']),
            "port": port,
            "temp_dir": tempfile.gettempdir(),
            "current_dir": os.getcwd(),
            "files_in_current_dir": os.listdir('.')
        }
        
        logger.info(f"Health check: {system_info}")
        return jsonify(system_info)
    except Exception as e:
        logger.error(f"Health check error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルがありません'}), 400
    
    files = request.files.getlist('file')
    if not files or files[0].filename == '':
        return jsonify({'error': 'ファイルが選択されていません'}), 400
    
    # 全てのレコードを保存するリスト
    all_records = []
    
    # プログレス情報を初期化
    global progress_data
    progress_data = {
        'current_file': 0,
        'total_files': len(files),
        'current_page': 0,
        'total_pages': 0,
        'filename': ''
    }
    
    total_files = len(files)
    
    # 各ファイルを処理
    for file_index, file in enumerate(files):
        # プログレス情報を更新
        progress_data['current_file'] = file_index + 1
        progress_data['filename'] = file.filename
        
        if file and allowed_file(file.filename):
            try:
                filename = secure_filename(file.filename)
                file_path = os.path.join(tempfile.gettempdir(), filename)
                file.save(file_path)
                
                # ファイルの拡張子を取得
                file_ext = os.path.splitext(filename)[1].lower()
                
                # PDFの場合は画像に変換して処理
                if file_ext == '.pdf':
                    images = convert_pdf_to_images(file_path)
                    progress_data['total_pages'] = len(images)
                    
                    for page_index, img in enumerate(images):
                        # ページ進捗状況を更新
                        progress_data['current_page'] = page_index + 1
                        logger.info(f"Processing file {progress_data['current_file']}/{total_files}: {filename} - Page {page_index+1}/{len(images)}")
                        
                        result = process_image(img)
                        if 'records' in result:
                            # 各レコードに必須キーが存在することを確認
                            for record in result['records']:
                                for key in ['payment_amount', 'withholding_tax']:
                                    if key not in record:
                                        record[key] = 0
                            all_records.extend(result['records'])
                else:
                    # 画像を直接処理
                    progress_data['total_pages'] = 1
                    progress_data['current_page'] = 1
                    logger.info(f"Processing file {progress_data['current_file']}/{total_files}: {filename}")
                    
                    img = Image.open(file_path)
                    result = process_image(img)
                    if 'records' in result:
                        # 各レコードに必須キーが存在することを確認
                        for record in result['records']:
                            for key in ['payment_amount', 'withholding_tax']:
                                if key not in record:
                                    record[key] = 0
                        all_records.extend(result['records'])
            except Exception as e:
                logger.error(f"処理エラー: {str(e)}")
                logger.error(traceback.format_exc())
                return jsonify({'error': f'処理エラー: {str(e)}'}), 500
            finally:
                # 一時ファイルを削除
                if os.path.exists(file_path):
                    os.remove(file_path)
    
    # 集計結果を作成
    aggregated_records = aggregate_records(all_records)
    
    # CSVファイルを作成（日本語ヘッダー）
    csv_file = io.StringIO()
    jp_fieldnames = ['区分', '細目', '支払金額', '源泉徴収税額', '支払者所在地', '支払者名称', '支払者電話番号']
    fieldnames = ['category', 'detail', 'payment_amount', 'withholding_tax', 'payer_address', 'payer_name', 'payer_tel']
    writer = csv.writer(csv_file)
    writer.writerow(jp_fieldnames)
    
    for record in all_records:
        # 金額がない場合はデフォルト値を設定
        payment_amount = record.get('payment_amount', 0) or 0
        withholding_tax = record.get('withholding_tax', 0) or 0
        
        writer.writerow([
            record['category'],
            record['detail'],
            payment_amount,
            withholding_tax,
            record['payer_address'],
            record['payer_name'],
            record['payer_tel']
        ])
    
    # 集計CSVファイルも作成（日本語ヘッダー）
    aggregated_csv = io.StringIO()
    jp_agg_fieldnames = ['区分', '細目', '支払金額', '源泉徴収税額', '支払者所在地', '支払者名称', '支払者電話番号', '件数']
    agg_writer = csv.writer(aggregated_csv)
    agg_writer.writerow(jp_agg_fieldnames)
    
    for record in aggregated_records:
        # 金額がない場合はデフォルト値を設定
        payment_amount = record.get('payment_amount', 0) or 0
        withholding_tax = record.get('withholding_tax', 0) or 0
        
        agg_writer.writerow([
            record['category'],
            record['detail'],
            payment_amount,
            withholding_tax,
            record['payer_address'],
            record['payer_name'],
            record['payer_tel'],
            record['count']
        ])
    
    # 結果を返す
    return jsonify({
        'records': all_records,
        'aggregated': aggregated_records,
        'csv': csv_file.getvalue(),
        'aggregated_csv': aggregated_csv.getvalue(),
        'processed_files': total_files
    })

def get_port():
    """環境に応じて適切なポートを取得する"""
    # Heroku環境ではPORT環境変数が設定される
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Using port: {port}")
    return port

if __name__ == '__main__':
    port = get_port()
    logger.info(f"Starting application on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

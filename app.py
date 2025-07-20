
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
import os
import json

app = Flask(__name__)
app.secret_key = 'votre_clé_secrète_très_sécurisée'

def get_db_connection():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect('bot_data.db', timeout=20.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            if attempt == max_retries - 1:
                raise e
            import time
            time.sleep(0.5)
    return None

@app.route('/')
def index():
    """Page d'accueil du panneau de configuration"""
    conn = get_db_connection()
    
    # Compter les utilisateurs avec XP
    cursor = conn.execute('SELECT COUNT(DISTINCT user_id) as user_count FROM user_levels')
    user_count = cursor.fetchone()['user_count']
    
    # Compter les serveurs configurés
    cursor = conn.execute('SELECT COUNT(*) as guild_count FROM guild_config')
    guild_count = cursor.fetchone()['guild_count']
    
    # Compter les avertissements
    cursor = conn.execute('SELECT COUNT(*) as warning_count FROM warnings')
    warning_count = cursor.fetchone()['warning_count']
    
    conn.close()
    
    return render_template('index.html', 
                         user_count=user_count, 
                         guild_count=guild_count, 
                         warning_count=warning_count)

@app.route('/config')
def config():
    """Page de configuration des serveurs"""
    conn = get_db_connection()
    guilds = conn.execute('SELECT * FROM guild_config').fetchall()
    conn.close()
    return render_template('config.html', guilds=guilds)

@app.route('/config/edit/<int:guild_id>')
def edit_config(guild_id):
    """Éditer la configuration d'un serveur"""
    conn = get_db_connection()
    guild = conn.execute('SELECT * FROM guild_config WHERE guild_id = ?', (guild_id,)).fetchone()
    level_roles = conn.execute('SELECT * FROM level_roles WHERE guild_id = ? ORDER BY level', (guild_id,)).fetchall()
    conn.close()
    return render_template('edit_config.html', guild=guild, level_roles=level_roles, guild_id=guild_id)

@app.route('/config/save', methods=['POST'])
def save_config():
    """Sauvegarder la configuration"""
    guild_id = request.form.get('guild_id')
    if not guild_id:
        flash('ID du serveur requis !', 'error')
        return redirect(url_for('config'))
    
    welcome_channel = request.form.get('welcome_channel', '').strip() or None
    goodbye_channel = request.form.get('goodbye_channel', '').strip() or None
    log_channel = request.form.get('log_channel', '').strip() or None
    level_up_channel = request.form.get('level_up_channel', '').strip() or None
    auto_role = request.form.get('auto_role', '').strip() or None
    mute_role = request.form.get('mute_role', '').strip() or None
    
    try:
        conn = get_db_connection()
        if conn is None:
            flash('Erreur de connexion à la base de données !', 'error')
            return redirect(url_for('config'))
        
        conn.execute('''
            INSERT OR REPLACE INTO guild_config 
            (guild_id, welcome_channel, goodbye_channel, log_channel, level_up_channel, auto_role, mute_role)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (guild_id, welcome_channel, goodbye_channel, log_channel, level_up_channel, auto_role, mute_role))
        conn.commit()
        conn.close()
        
        flash('Configuration sauvegardée avec succès !', 'success')
    except sqlite3.OperationalError as e:
        flash(f'Erreur lors de la sauvegarde : {str(e)}', 'error')
    
    return redirect(url_for('config'))

@app.route('/levels')
def levels():
    """Page de gestion des niveaux"""
    conn = get_db_connection()
    top_users = conn.execute('''
        SELECT user_id, guild_id, xp, level, messages 
        FROM user_levels 
        ORDER BY xp DESC 
        LIMIT 50
    ''').fetchall()
    conn.close()
    return render_template('levels.html', top_users=top_users)

@app.route('/warnings')
def warnings():
    """Page de gestion des avertissements"""
    conn = get_db_connection()
    warnings = conn.execute('''
        SELECT w.*, COUNT(*) as total_warnings
        FROM warnings w
        GROUP BY w.user_id, w.guild_id
        ORDER BY total_warnings DESC, w.timestamp DESC
        LIMIT 100
    ''').fetchall()
    conn.close()
    return render_template('warnings.html', warnings=warnings)

@app.route('/level_roles')
def level_roles():
    """Page de gestion des rôles de niveau"""
    conn = get_db_connection()
    level_roles = conn.execute('''
        SELECT * FROM level_roles 
        ORDER BY guild_id, level
    ''').fetchall()
    conn.close()
    return render_template('level_roles.html', level_roles=level_roles)

@app.route('/level_roles/add', methods=['POST'])
def add_level_role():
    """Ajouter un rôle de niveau"""
    guild_id = request.form['guild_id']
    level = request.form['level']
    role_id = request.form['role_id']
    
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?)', 
                    (guild_id, level, role_id))
        conn.commit()
        flash('Rôle de niveau ajouté avec succès !', 'success')
    except sqlite3.IntegrityError:
        flash('Un rôle existe déjà pour ce niveau sur ce serveur !', 'error')
    conn.close()
    
    return redirect(url_for('level_roles'))

@app.route('/level_roles/delete/<int:guild_id>/<int:level>')
def delete_level_role(guild_id, level):
    """Supprimer un rôle de niveau"""
    conn = get_db_connection()
    conn.execute('DELETE FROM level_roles WHERE guild_id = ? AND level = ?', (guild_id, level))
    conn.commit()
    conn.close()
    flash('Rôle de niveau supprimé !', 'success')
    return redirect(url_for('level_roles'))

@app.route('/api/stats')
def api_stats():
    """API pour les statistiques"""
    conn = get_db_connection()
    
    # Statistiques générales
    stats = {
        'total_users': conn.execute('SELECT COUNT(DISTINCT user_id) FROM user_levels').fetchone()[0],
        'total_messages': conn.execute('SELECT SUM(messages) FROM user_levels').fetchone()[0] or 0,
        'total_xp': conn.execute('SELECT SUM(xp) FROM user_levels').fetchone()[0] or 0,
        'total_warnings': conn.execute('SELECT COUNT(*) FROM warnings').fetchone()[0],
        'configured_guilds': conn.execute('SELECT COUNT(*) FROM guild_config').fetchone()[0]
    }
    
    conn.close()
    return jsonify(stats)

@app.route('/moderation')
def moderation():
    """Page de modération"""
    conn = get_db_connection()
    
    # Statistiques de modération
    warning_count = conn.execute('SELECT COUNT(*) FROM warnings').fetchone()[0]
    ban_count = 0  # À implémenter avec une table de bans
    mute_count = 0  # À implémenter
    action_count = 0  # Actions du jour
    
    # Dernières infractions (warnings pour l'instant)
    infractions = conn.execute('''
        SELECT w.*, 'WARN' as type
        FROM warnings w
        ORDER BY w.timestamp DESC
        LIMIT 50
    ''').fetchall()
    
    # Utilisateurs bannis (simulé pour l'instant)
    banned_users = []
    
    conn.close()
    
    return render_template('moderation.html', 
                         warning_count=warning_count,
                         ban_count=ban_count,
                         mute_count=mute_count,
                         action_count=action_count,
                         infractions=infractions,
                         banned_users=banned_users)

@app.route('/music')
def music():
    """Page de gestion musicale"""
    return render_template('music.html')

@app.route('/api/music/search', methods=['POST'])
def api_music_search():
    """API de recherche musicale"""
    data = request.get_json()
    query = data.get('query', '')
    search_type = data.get('type', 'youtube')
    
    # Simulation de résultats de recherche
    results = [
        {
            'title': f'Résultat 1 pour "{query}"',
            'artist': 'Artiste Example',
            'duration': '3:45',
            'url': 'https://example.com/song1'
        },
        {
            'title': f'Résultat 2 pour "{query}"',
            'artist': 'Autre Artiste',
            'duration': '4:12',
            'url': 'https://example.com/song2'
        }
    ]
    
    return jsonify({'success': True, 'results': results})

@app.route('/api/music/pause', methods=['POST'])
def api_music_pause():
    """API pour mettre en pause"""
    return jsonify({'success': True, 'message': 'Musique en pause'})

@app.route('/api/music/resume', methods=['POST'])
def api_music_resume():
    """API pour reprendre"""
    return jsonify({'success': True, 'message': 'Musique reprise'})

@app.route('/api/music/stop', methods=['POST'])
def api_music_stop():
    """API pour arrêter"""
    return jsonify({'success': True, 'message': 'Musique arrêtée'})

@app.route('/api/music/skip', methods=['POST'])
def api_music_skip():
    """API pour passer à la suivante"""
    return jsonify({'success': True, 'message': 'Musique suivante'})

@app.route('/api/music/volume', methods=['POST'])
def api_music_volume():
    """API pour changer le volume"""
    data = request.get_json()
    volume = data.get('volume', 50)
    return jsonify({'success': True, 'message': f'Volume défini à {volume}%'})

@app.route('/api/music/queue')
def api_music_queue():
    """API pour obtenir la file d'attente"""
    # Simulation d'une file d'attente
    queue = [
        {'title': 'Chanson 1', 'artist': 'Artiste 1'},
        {'title': 'Chanson 2', 'artist': 'Artiste 2'}
    ]
    return jsonify({'success': True, 'queue': queue})

@app.route('/api/music/clear-queue', methods=['POST'])
def api_music_clear_queue():
    """API pour vider la file d'attente"""
    return jsonify({'success': True, 'message': 'File d\'attente vidée'})

@app.route('/api/music/connect', methods=['POST'])
def api_music_connect():
    """API pour connecter le bot au salon vocal"""
    try:
        # Créer un fichier de commande pour le bot
        with open('music_commands.txt', 'a') as f:
            f.write('CONNECT\n')
        return jsonify({'success': True, 'message': 'Commande de connexion envoyée au bot'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'})

@app.route('/api/music/disconnect', methods=['POST'])
def api_music_disconnect():
    """API pour déconnecter le bot du salon vocal"""
    try:
        # Créer un fichier de commande pour le bot
        with open('music_commands.txt', 'a') as f:
            f.write('DISCONNECT\n')
        return jsonify({'success': True, 'message': 'Commande de déconnexion envoyée au bot'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'})

@app.route('/api/music/play', methods=['POST'])
def api_music_play():
    """API pour jouer une musique"""
    data = request.get_json()
    url = data.get('url')
    title = data.get('title')
    
    try:
        # Créer un fichier de commande pour le bot
        with open('music_commands.txt', 'a') as f:
            f.write(f'PLAY|{url}|{title}\n')
        return jsonify({'success': True, 'message': f'Commande de lecture envoyée: {title}'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'})

@app.route('/api/music/add', methods=['POST'])
def api_music_add():
    """API pour ajouter à la file d'attente"""
    data = request.get_json()
    url = data.get('url')
    title = data.get('title')
    
    try:
        # Créer un fichier de commande pour le bot
        with open('music_commands.txt', 'a') as f:
            f.write(f'ADD|{url}|{title}\n')
        return jsonify({'success': True, 'message': f'Commande d\'ajout envoyée: {title}'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Erreur: {str(e)}'})

@app.route('/api/music/status')
def api_music_status():
    """API pour le statut musical"""
    return jsonify({
        'success': True,
        'status': 'En ligne',
        'voice_channel': 'Salon général',
        'voice_users': 3,
        'current_song': 'Aucune musique',
        'is_playing': False
    })

@app.route('/messages')
def messages():
    """Page de gestion des messages"""
    return render_template('messages.html')

@app.route('/api/send-message', methods=['POST'])
def api_send_message():
    """API pour envoyer un message via Discord"""
    data = request.get_json()
    
    channel_id = data.get('channel_id')
    message_type = data.get('type', 'simple')
    
    if not channel_id:
        return jsonify({'success': False, 'error': 'ID du salon requis'})
    
    try:
        # Créer un fichier de commande pour le bot Discord
        with open('message_commands.txt', 'a', encoding='utf-8') as f:
            if message_type == 'simple':
                content = data.get('content', '')
                f.write(f'SEND_MESSAGE|{channel_id}|{content}\n')
            else:
                embed_data = data.get('embed', {})
                embed_json = json.dumps(embed_data, ensure_ascii=False)
                f.write(f'SEND_EMBED|{channel_id}|{embed_json}\n')
        
        return jsonify({'success': True, 'message': 'Message envoyé au bot Discord'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/user-info/<int:user_id>')
def api_user_info(user_id):
    """API pour les informations utilisateur"""
    conn = get_db_connection()
    
    # Récupérer les infos utilisateur
    user_data = conn.execute('''
        SELECT xp, level, messages FROM user_levels 
        WHERE user_id = ? 
        LIMIT 1
    ''', (user_id,)).fetchone()
    
    warnings_count = conn.execute('''
        SELECT COUNT(*) FROM warnings WHERE user_id = ?
    ''', (user_id,)).fetchone()[0]
    
    conn.close()
    
    if user_data:
        return jsonify({
            'success': True,
            'user': {
                'name': f'Utilisateur #{user_id}',
                'level': user_data['level'],
                'xp': user_data['xp'],
                'messages': user_data['messages'],
                'warnings': warnings_count,
                'banned': False,
                'last_activity': 'Inconnue'
            }
        })
    else:
        return jsonify({'success': False, 'error': 'Utilisateur non trouvé'})

@app.route('/api/unban', methods=['POST'])
def api_unban():
    """API pour débannir un utilisateur"""
    data = request.get_json()
    user_id = data.get('user_id')
    reason = data.get('reason', 'Débannissement via interface web')
    
    # Ici vous pourriez envoyer une commande au bot Discord
    return jsonify({'success': True, 'message': f'Utilisateur {user_id} débanni'})

if __name__ == '__main__':
    print("🌐 Démarrage du panneau de configuration web...")
    print("📋 Fonctionnalités du panneau :")
    print("   • Configuration des serveurs")
    print("   • Gestion des niveaux et rôles")
    print("   • Visualisation des avertissements")
    print("   • Statistiques en temps réel")
    print("\n🚀 Interface disponible sur : http://0.0.0.0:5000")
    
    app.run(host='0.0.0.0', port=5000, debug=True)
